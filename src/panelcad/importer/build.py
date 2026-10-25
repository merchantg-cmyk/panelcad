"""Build native sheets through Session, the same path the drawing agent uses."""

from __future__ import annotations

from pathlib import Path

import math
import re
from typing import Any

from panelcad.api.session import Session
from panelcad.core.geometry import match_page_size, rotate_point
from panelcad.importer.models import LAYOUT_KINDS, LOW_CONFIDENCE
from panelcad.importer.symbols import print_tag_placeable, resolve_symbol


def build_session(
    stitched: dict[str, Any] | Any,
    *,
    catalog: Any = None,
    parts: Any = None,
    name: str = "",
    photo_targets: list[dict[str, Any]] | None = None,
    layout_mode: str = "preserve",
    target_standard: str = "auto",
) -> tuple[Session, dict[str, Any]]:
    if not isinstance(stitched, dict):
        import json as _json

        root_dir: Path | None = None
        if isinstance(stitched, (str, Path)):
            root_dir = Path(stitched)
        elif hasattr(stitched, "ws") and hasattr(stitched.ws, "root"):
            root_dir = Path(stitched.ws.root)
        elif hasattr(stitched, "root"):
            root_dir = Path(stitched.root)
        loaded_dict: dict[str, Any] | None = None
        if root_dir is not None and root_dir.is_dir():
            for fname in ("facts.json", "stitch.json"):
                cand = root_dir / fname
                if cand.is_file():
                    loaded_dict = _json.loads(cand.read_text(encoding="utf-8"))
                    break
        if loaded_dict is None and hasattr(stitched, "stitch"):
            loaded_dict = stitched.stitch()
        stitched = loaded_dict or {}
    block = stitched.get("title_block") or {}
    title = name or block.get("project") or block.get("title") or block.get("drawing_number") or "Imported print"
    session = Session.create(title, sheet_id="import-hold", title="hold")
    if catalog is not None:
        session.catalog = catalog
    if parts is not None:
        session.parts = parts
    if "standard" in stitched:
        raw_standard = str(stitched.get("standard") or "")
    else:
        raw_standard = str(block.get("standard") or "IEC")
    # mixed and unknown must not be normalized into IEC.
    standard = raw_standard if raw_standard in {"NEMA/ANSI", "IEC"} else ""
    style = stitched.get("drawing_style") or ("ladder" if standard == "NEMA/ANSI" else "schematic")
    fields: dict[str, Any] = {"drawing_style": style, "name": title}
    if standard:
        fields["standard"] = standard
    for key in ("drawing_number", "rev", "company", "address", "customer", "location", "drawn_by", "scale", "date", "rev_description"):
        if block.get(key):
            fields[key] = block[key]
    session.set_title_block(**fields)

    eff_mode = str(
        layout_mode
        if layout_mode and layout_mode != "preserve"
        else (stitched.get("layout_mode") or layout_mode or "preserve")
    ).strip().lower()
    if eff_mode in {"ladder", "clean_ladder", "clean"}:
        from panelcad.importer.ladder_synth import synthesize_imported_ladder

        session = synthesize_imported_ladder(
            stitched,
            target_standard=target_standard,
            catalog=catalog,
            parts=parts,
            name=title,
        )
        report: dict[str, Any] = {"placed": [], "wires": [], "skipped": [], "unresolved_wires": [], "photos": []}
        existing_ids = {s.id for s in session.project.sheets}
        for sheet in stitched.get("sheets") or []:
            kind = sheet.get("kind") or "schematic"
            if kind in LAYOUT_KINDS and sheet.get("id") not in existing_ids:
                created = session.add_sheet(sheet["id"], title=sheet.get("title") or sheet["id"], kind=kind)
                _apply_page_size(created, sheet)
                _place_sheet(session, created.id, sheet, report)
                existing_ids.add(created.id)
        placed_keys = {(r.get("sheet"), r.get("tag")) for r in report["placed"]}
        for sh in session.project.sheets:
            for comp in sh.components:
                if (sh.id, comp.id) not in placed_keys:
                    report["placed"].append(
                        {
                            "tag": comp.id,
                            "library_id": comp.library_id,
                            "sheet": sh.id,
                            "confidence": float((comp.properties or {}).get("print_import_confidence", 0.92)),
                        }
                    )
            for wire in sh.wires:
                report["wires"].append(
                    {
                        "number": wire.number or wire.id,
                        "sheet": sh.id,
                        "from": wire.frm,
                        "to": wire.to,
                    }
                )
        if session.project.sheets:
            session.set_active_sheet(session.project.sheets[0].id)
        _apply_photo_targets(session, stitched, photo_targets or [], report)
        _stamp_properties(session, stitched, report, layout_mode="ladder", target_standard=target_standard)
        return session, report

    from panelcad.importer.tile import collapse_devices_across_sheets

    collapse_devices_across_sheets(list(stitched.get("sheets") or []))
    report = {"placed": [], "wires": [], "skipped": [], "unresolved_wires": [], "photos": []}
    sheet_ids: list[str] = []
    # A layout tag that did not match ink is the device already placed on the
    # schematic. Place those sheets after the schematics so the twin exists.
    pending_layout: list[tuple[str, dict[str, Any]]] = []
    for sheet in stitched.get("sheets") or []:
        kind = sheet.get("kind") or "schematic"
        if kind not in {"schematic", "panel_layout", "door_layout", "terminal", "network", "io"}:
            kind = "schematic"
        created = session.add_sheet(sheet["id"], title=sheet.get("title") or sheet["id"], kind=kind)
        page_standard = str(sheet.get("standard") or "")
        if page_standard in {"NEMA/ANSI", "IEC"}:
            created.standard = page_standard
        _apply_page_size(created, sheet)
        sheet_ids.append(created.id)
        if sheet.get("description"):
            created.description = str(sheet["description"])
        if sheet.get("drawing_style"):
            created.drawing_style = sheet["drawing_style"]
        if kind in LAYOUT_KINDS:
            pending_layout.append((created.id, sheet))
            continue
        _place_sheet(session, created.id, sheet, report)
        _wire_sheet(session, created.id, sheet, report)
    for sheet_id, sheet in pending_layout:
        _place_sheet(session, sheet_id, sheet, report)
    if "import-hold" in {s.id for s in session.project.sheets} and len(session.project.sheets) > 1:
        session.delete_sheet("import-hold")
    if sheet_ids:
        session.set_active_sheet(sheet_ids[0])
    _apply_photo_targets(session, stitched, photo_targets or [], report)
    _stamp_properties(session, stitched, report, layout_mode="preserve", target_standard=target_standard)
    return session, report


def _apply_page_size(created: Any, sheet: dict[str, Any]) -> None:
    """11×17 at 300 dpi is tabloid. It does not collapse to A3."""

    try:
        width = float(sheet.get("width_mm") or 0)
        height = float(sheet.get("height_mm") or 0)
    except (TypeError, ValueError):
        return
    if width < 20 or height < 20:
        return
    name, orient = match_page_size(width, height)
    page = created.page
    page.size = name
    page.orientation = orient
    if name == "custom":
        page.width_mm = width
        page.height_mm = height


def link_wire_ends(devices: list[dict[str, Any]], wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill from_tag/to_tag from geometry when the crop did not name the ends."""

    if len(devices) < 2:
        return wires
    edges = _mst(devices)
    linked = []
    for wire in wires:
        item = dict(wire)
        if item.get("from_tag") and item.get("to_tag"):
            linked.append(item)
            continue
        if item.get("x") is None or not edges:
            linked.append(item)
            continue
        best = None
        best_d = 1e9
        for a, b in edges:
            dist = _seg_dist(float(item["x"]), float(item["y"]), a, b)
            if dist < best_d:
                best_d = dist
                best = (a, b)
        if best is not None and best_d <= 90.0:
            item["from_tag"] = best[0]["tag"]
            item["to_tag"] = best[1]["tag"]
        linked.append(item)
    return linked


def _place_sheet(session: Session, sheet_id: str, sheet: dict[str, Any], report: dict[str, Any]) -> None:
    from panelcad.importer.raster import orient_ladder_devices
    from panelcad.library.panel import LayoutNotReadyError

    page_width = 0.0
    try:
        page_width = float(session.project.sheet(sheet_id).page.dimensions()[0])
    except (AttributeError, KeyError, TypeError, ValueError):
        page_width = float(sheet.get("width_mm") or 0)
    orient_ladder_devices(
        list(sheet.get("devices") or []),
        session.catalog,
        spread=True,
        page_width=page_width,
    )
    from panelcad.importer.tile import mark_secondary_occurrences

    mark_secondary_occurrences(
        list(sheet.get("devices") or []),
        list(sheet.get("wires") or []),
        session.catalog,
    )
    session.set_active_sheet(sheet_id)
    kind = sheet.get("page_kind") or ""
    snap = kind in LAYOUT_KINDS
    for device in sheet.get("devices") or []:
        if device.get("rejected") or device.get("endpoint"):
            continue
        tag = str(device.get("tag") or "").strip()
        if device.get("secondary"):
            # The box stays on the sheet for rung geometry. It is not a second component.
            continue
        if not tag or device.get("unreadable"):
            report["skipped"].append({"tag": tag, "reason": "unreadable", "sheet": sheet_id})
            continue
        if device.get("tag_unparsed") or device.get("title_ink"):
            reason = "tag_unparsed" if device.get("tag_unparsed") else "title_block"
            report["skipped"].append({"tag": tag, "reason": reason, "sheet": sheet_id})
            continue
        if device.get("unlocalised"):
            report["skipped"].append({"tag": tag, "reason": "unlocalised", "sheet": sheet_id})
            continue
        if _layout_furniture_tag(tag) and kind in LAYOUT_KINDS:
            # A rail or duct id on a layout. On a schematic the same text stays a part tag.
            report["skipped"].append({"tag": tag, "reason": "layout_label", "sheet": sheet_id})
            continue
        if device.get("contact_uncertain"):
            report["skipped"].append(
                {
                    "tag": tag,
                    "reason": "needs_info",
                    "sheet": sheet_id,
                    "detail": "The contact drawing does not say NO or NC.",
                }
            )
            continue
        if not print_tag_placeable(device):
            # Not a tag this sheet can build. Kept as a needs-info mark, with no millimetres.
            _keep_read_needs_info(
                session,
                sheet_id,
                tag,
                device,
                report,
                "NOT FOR BUILD. No millimetres. Kept off the BOM, the quote, and cutouts.",
            )
            continue
        if snap and device.get("guess") and str(device.get("source") or "") in {"", "ocr"}:
            _keep_read_needs_info(
                session,
                sheet_id,
                tag,
                device,
                report,
                "NOT FOR BUILD. No millimetres. Kept off the BOM, the quote, and cutouts.",
            )
            continue
        standard = _sheet_import_standard(sheet, session)
        nema_print = standard == "NEMA/ANSI"
        unknown_print = standard in {"", "mixed"}
        confirmed = _library_confirmed(device)
        explicit = _geometry_library(device)
        if snap and not explicit:
            twin = _find_component(session, tag)
            if twin is not None:
                explicit = twin.library_id
        # A NEMA page does not keep an IEC id the model named. Mixed or unknown
        # pages keep an id only when a person or the catalog confirmed it.
        if nema_print and explicit and not explicit.startswith("nema."):
            explicit = ""
        elif unknown_print and explicit and not confirmed:
            explicit = ""
        resolved = resolve_symbol(
            session.catalog,
            tag=tag,
            type_text=str(device.get("type") or device.get("symbol") or ""),
            standard=standard if not unknown_print else "",
            library_id=explicit,
        )
        library_id = explicit or resolved["library_id"]
        if library_id and library_id not in session.catalog.devices:
            library_id = resolved["library_id"]
        if unknown_print and not confirmed:
            library_id = ""
        elif nema_print and library_id and not str(library_id).startswith("nema."):
            library_id = ""
        guessed = bool(resolved["guess"]) and not explicit
        library_id, guessed, unknown = _family_or_unknown(
            session,
            tag,
            library_id,
            resolved,
            device,
            standard if not unknown_print else "",
            guessed,
        )
        if unknown:
            _place_unknown_device(session, sheet_id, tag, device, report)
            continue
        if not library_id:
            detail = "No NEMA library part. Kept off the BOM and the quote." if nema_print else ""
            if nema_print or unknown_print:
                _keep_read_needs_info(session, sheet_id, tag, device, report, detail or "NOT FOR BUILD. No millimetres. Kept off the BOM, the quote, and cutouts.")
            else:
                report["skipped"].append(
                    {
                        "tag": tag,
                        "reason": "no_symbol",
                        "sheet": sheet_id,
                        "detail": detail,
                    }
                )
            continue
        device["library_id"] = library_id
        device["library_guess"] = guessed
        if guessed:
            device["confidence"] = min(float(device.get("confidence") or 0.9), float(resolved["confidence"]))
        x = float(device.get("x") or 40.0)
        y = float(device.get("y") or 40.0)
        props = {
            "print_import_confidence": round(float(device.get("confidence") or 0), 3),
            "print_import_crop": str(device.get("crop") or ""),
            "print_import_page": str(device.get("page_id") or sheet.get("page_id") or ""),
            "print_import_source": str(device.get("source") or ""),
        }
        if device.get("catalog") and device.get("catalog_matched"):
            props["catalog"] = device["catalog"]
            if device.get("manufacturer"):
                props["manufacturer"] = device["manufacturer"]
        rotation = int(device.get("rotation") or 0) % 360
        if rotation not in {0, 90, 180, 270}:
            rotation = 0
        if _sheet_is_layout(session, sheet_id):
            block = _unready_layout_reason(session, library_id)
            if block:
                _hold_unready_layout(session, sheet_id, tag, library_id, x, y, report, block)
                continue
        try:
            comp = session.place(
                library_id,
                ref=tag,
                x=x,
                y=y,
                rotation=rotation,
                sheet_id=sheet_id,
                properties=props,
                snap=snap,
                scale=float(device.get("scale") or 1.0),
            )
        except LayoutNotReadyError as exc:
            _hold_unready_layout(session, sheet_id, tag, library_id, x, y, report, str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — one bad symbol does not drop the page
            if "already exists" in str(exc).lower():
                existing = _find_component(session, tag)
                if existing is not None:
                    report["placed"].append(
                        {
                            "tag": existing.id,
                            "library_id": existing.library_id,
                            "sheet": sheet_id,
                            "guess": bool(resolved["guess"]),
                            "merged": True,
                        }
                    )
                    continue
            report["skipped"].append({"tag": tag, "reason": str(exc), "sheet": sheet_id, "library_id": library_id})
            continue
        report["placed"].append({"tag": comp.id, "library_id": comp.library_id, "sheet": sheet_id, "guess": bool(resolved["guess"])})


def stacked_tag_ids(points: list[tuple[str, float, float]], limit: float = 3.0) -> set[str]:
    """Tags that sit on the same spot. A wire cannot tell which one it reached."""

    stacked: set[str] = set()
    for index, (left, lx, ly) in enumerate(points):
        for right, rx, ry in points[index + 1 :]:
            if math.hypot(lx - rx, ly - ry) <= limit:
                stacked.add(left)
                stacked.add(right)
    return stacked


def _wire_sheet(session: Session, sheet_id: str, sheet: dict[str, Any], report: dict[str, Any]) -> None:
    wires = [dict(item) for item in (sheet.get("wires") or []) if isinstance(item, dict)]
    _anchor_needs_info_ends(session, sheet_id, sheet, wires, report)
    complete_conductor_ends(session, sheet_id, wires)
    _bind_loose_labels(session, sheet_id, wires)
    # Numbered-rung gaps are a NEMA/JIC ladder. An IEC or unknown page does not invent them.
    if _import_mode(sheet, session) == "nema":
        complete_numbered_rungs(
            wires,
            _rung_occurrences(session, sheet_id, list(sheet.get("devices") or [])),
        )
    bus_labels = [label for label in (_bus_label(wire) for wire in wires) if label]
    _adopt_printed_rails(session, sheet_id, wires)
    tied: set[str] = set()
    used: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for wire in wires:
        if wire.get("rejected"):
            continue
        if (
            wire.get("connect_by_hand")
            and not str(wire.get("from_tag") or "").strip()
            and not str(wire.get("to_tag") or "").strip()
        ):
            number = str(wire.get("number") or "").strip()
            if number:
                report["unresolved_wires"].append(_unconnected_label(wire, number, sheet_id))
            continue
        if wire.get("provisional"):
            number = str(wire.get("number") or "").strip()
            if not _realize_provisional_pins(session, sheet_id, wire, used):
                wire["connect_by_hand"] = True
                wire["ambiguous"] = True
                wire["from_pin"] = ""
                wire["to_pin"] = ""
            elif number:
                report["unresolved_wires"].append(
                    {
                        "number": number,
                        "reason": "provisional",
                        "sheet": sheet_id,
                        "x": wire.get("x"),
                        "y": wire.get("y"),
                        "confidence": wire.get("confidence") or 0.55,
                    }
                )
        if wire.get("rung_uncertain") and not _fully_pinned(wire):
            number = str(wire.get("number") or "").strip()
            _remember_unresolved(
                report,
                wire,
                {
                    "number": number,
                    "reason": "needs_info",
                    "sheet": sheet_id,
                    "confidence": wire.get("confidence") or 0,
                },
            )
            continue
        label = _bus_label(wire)
        number = str(wire.get("number") or "").strip() or label
        if label and not _fully_pinned(wire):
            if label not in tied:
                _tie_bus(
                    session,
                    sheet_id,
                    label,
                    bus_labels,
                    report,
                    used,
                    distance_mm=wire.get("label_distance_mm"),
                    confidence=wire.get("confidence"),
                    source=wire,
                )
                tied.add(label)
            continue
        if label and not number:
            number = label
        pinned = bool(wire.get("from_pin") and wire.get("to_pin") and wire.get("from_tag") and wire.get("to_tag"))
        if wire.get("unreadable") or (not number and not pinned):
            _remember_unresolved(report, wire, {"number": number, "reason": "unreadable", "sheet": sheet_id})
            continue
        role, rail = _potential(number)
        if role and _open_end(wire):
            _wire_potential(session, sheet_id, wire, number, role, rail, report)
            continue
        if wire.get("label_unmatched") and not _fully_pinned(wire):
            _remember_unresolved(report, wire, _parked_label(wire, number, sheet_id, "unmatched"))
            continue
        missing_pins = not wire.get("from_pin") or not wire.get("to_pin")
        if missing_pins and (wire.get("stub") or wire.get("attached")):
            reason = "wire stub" if wire.get("stub") else "attached"
            _remember_unresolved(report, wire, _parked_label(wire, number, sheet_id, reason))
            continue
        if number and not str(wire.get("number") or "").strip():
            wire["number"] = number
        if wire.get("ambiguous") or not wire.get("from_pin") or not wire.get("to_pin"):
            wire["ambiguous"] = True
            wire["confidence"] = min(float(wire.get("confidence") or 0.4), 0.45)
            _remember_unresolved(report, wire, _unconnected_label(wire, number, sheet_id))
            continue
        rail_link = _rail_device_link(wire)
        if rail_link is not None:
            rail, device_pin = rail_link
            tag, _, pin = device_pin.partition(".")
            edge = tuple(sorted(((tag, pin), (rail, rail))))
            # Overlapping tiles can read one conductor twice. Draw it once.
            if edge not in used:
                try:
                    _import_wire(session, device_pin, rail, number=number, sheet_id=sheet_id)
                except Exception:  # noqa: BLE001 — one rail end stays in the review list
                    report["unresolved_wires"].append(
                        {"number": number, "reason": "ambiguous", "sheet": sheet_id, "confidence": wire.get("confidence") or 0}
                    )
                    continue
                used.add(edge)
                report["wires"].append({"number": number, "sheet": sheet_id, "from": device_pin, "to": rail})
            continue
        if _rejected_self_wire(session, sheet_id, wire) or _named_pin_missing(session, sheet_id, wire):
            _remember_unresolved(
                report,
                wire,
                {"number": number, "reason": "needs_info", "sheet": sheet_id, "confidence": wire.get("confidence") or 0},
            )
            continue
        pair = _connect_traced(session, sheet_id, wire, used)
        if wire.get("duplicate_end"):
            continue
        if pair is None:
            wire["ambiguous"] = True
            wire["connect_by_hand"] = True
            wire["confidence"] = min(float(wire.get("confidence") or 0.4), 0.45)
            _remember_unresolved(report, wire, _unconnected_label(wire, number, sheet_id))
            continue
        report["wires"].append({"number": number, "sheet": sheet_id, "from": pair[0], "to": pair[1]})
    _connect_direct_mounts(session, sheet_id, report, used)
    _release_blank_imports(session, sheet_id)


# Phase conductors stay wires. Only these names are sheet rails.
_HOT_POTENTIALS = {"24V": "24V", "+24V": "24V", "24VDC": "24VDC"}
_RETURN_POTENTIALS = {"0V": "0V", "0VDC": "0VDC"}


def _potential(number: str) -> tuple[str, str]:
    token = number.strip().upper()
    if token in _HOT_POTENTIALS:
        return "hot", _HOT_POTENTIALS[token]
    if token in _RETURN_POTENTIALS:
        return "return", _RETURN_POTENTIALS[token]
    return "", ""


def _printed_rail_name(tag: str, pin: str) -> str:
    """A traced end whose tag and pin are the rail name (L1, L2, L+, 0V)."""

    from panelcad.importer.models import CONVENTIONAL_WIRES

    text = str(tag or "").strip().upper()
    other = str(pin or "").strip().upper()
    if not text or text != other or text.isdigit():
        return ""
    if text in CONVENTIONAL_WIRES or re.fullmatch(r"[A-Z]{1,3}\+?", text):
        return text
    return ""


def _rail_device_link(wire: dict[str, Any]) -> tuple[str, str] | None:
    """One device pin and one printed rail. Both device pins stay a normal wire."""

    rails: list[str] = []
    devices: list[str] = []
    for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
        tag = str(wire.get(tag_key) or "").strip()
        pin = str(wire.get(pin_key) or "").strip()
        rail = _printed_rail_name(tag, pin)
        if rail:
            rails.append(rail)
        elif tag and pin:
            devices.append(f"{tag}.{pin}")
    if len(rails) == 1 and len(devices) == 1:
        return rails[0], devices[0]
    return None


def _adopt_printed_rails(session: Session, sheet_id: str, wires: list[dict[str, Any]]) -> None:
    """The names on the drawn rails become this sheet's hot and return."""

    names: list[str] = []
    for wire in wires:
        for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
            rail = _printed_rail_name(str(wire.get(tag_key) or ""), str(wire.get(pin_key) or ""))
            if rail and rail not in names:
                names.append(rail)
    if not names:
        return
    # L2 is the return only on a two-rail control sheet. L1/L2/L3 is a power
    # sheet; those names are phase conductors and do not rewrite the rails.
    phases = [name for name in names if name in {"L1", "L2", "L3"}]
    if len(phases) >= 3:
        return
    hot_names = {"L1", "L+", "L", "24V", "24VDC", "120V", "120VAC", "240V", "480V"}
    return_names = {"N", "0V", "0VDC", "NEUTRAL", "COM", "M"}
    sheet = session.project.sheet(sheet_id)
    if set(names) == {"L1", "L2"}:
        sheet.rail_hot = "L1"
        sheet.rail_return = "L2"
        return
    if len(names) == 1:
        if names[0] in return_names:
            sheet.rail_return = names[0]
        elif names[0] in hot_names:
            sheet.rail_hot = names[0]
        return
    hot = next((name for name in names if name in hot_names), "")
    ret = next((name for name in names if name in return_names and name != hot), "")
    if hot and ret:
        sheet.rail_hot = hot
        sheet.rail_return = ret


def _open_end(wire: dict[str, Any]) -> bool:
    """A potential becomes a rail when one end is not a real device pin.

    A vector wire that already joins two pins keeps that connection.
    """

    real = 0
    for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
        tag = str(wire.get(tag_key) or "").strip()
        pin = str(wire.get(pin_key) or "").strip()
        if tag and pin and not tag.upper().startswith("WN"):
            real += 1
    return real < 2


def _wire_potential(
    session: Session,
    sheet_id: str,
    wire: dict[str, Any],
    number: str,
    role: str,
    rail: str,
    report: dict[str, Any],
) -> None:
    """Connect each real device pin to the potential rail. No placeholder device."""

    sheet = session.project.sheet(sheet_id)
    if role == "hot":
        sheet.rail_hot = rail
    else:
        sheet.rail_return = rail
    pins: list[str] = []
    for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
        tag = str(wire.get(tag_key) or "").strip()
        pin = str(wire.get(pin_key) or "").strip()
        if not tag or not pin or tag.upper().startswith("WN"):
            continue
        pins.append(f"{tag}.{pin}")
    if not pins:
        _remember_unresolved(
            report,
            wire,
            {"number": number, "reason": "potential", "sheet": sheet_id, "confidence": wire.get("confidence") or 0},
        )
        return
    placed = False
    for pin in pins:
        try:
            _import_wire(session, pin, rail, net=number, number=number, sheet_id=sheet_id)
        except Exception:  # noqa: BLE001 — one end stays in the review list
            continue
        report["wires"].append({"number": number, "sheet": sheet_id, "from": pin, "to": rail})
        placed = True
    if not placed:
        _remember_unresolved(
            report,
            wire,
            {"number": number, "reason": "potential", "sheet": sheet_id, "confidence": wire.get("confidence") or 0},
        )


def _import_wire(session: Session, frm: str, to: str, **kwargs: Any) -> Any:
    """Connect an imported conductor. A blank printed number stays blank.

    ``session.wire`` fills an empty number from the auto-numberer. A print that
    did not show a number must not gain a different one.
    """

    printed = str(kwargs.get("number") or "").strip()
    item = session.wire(frm, to, **kwargs)
    if item is not None and not printed:
        # Stay blank so this import does not invent a number, and stay unlocked
        # so a later Number wires can fill it. A later wire() in this same
        # import runs the auto-numberer again; the sheet pass clears those.
        item.number = ""
        item.number_locked = False
        blanks = getattr(session, "_import_blank_ids", None)
        if blanks is None:
            blanks = set()
            session._import_blank_ids = blanks
        blanks.add(item.id)
    return item


def _release_blank_imports(session: Session, sheet_id: str) -> None:
    """Drop numbers the auto-numberer added to wires the print left blank.

    A later sheet's ``wire()`` runs the auto-numberer over the whole project,
    so every blank import on every sheet is cleared again. The ids stay until
    the import finishes; the numbers stay unlocked for Number wires.
    """

    _ = sheet_id
    blanks = getattr(session, "_import_blank_ids", None)
    if not blanks:
        return
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if wire.id in blanks:
                wire.number = ""
                wire.number_locked = False


def _connect_traced(
    session: Session,
    sheet_id: str,
    wire: dict[str, Any],
    used: set[tuple[tuple[str, str], tuple[str, str]]],
) -> tuple[str, str] | None:
    """Connect the pins the trace named. A nearby terminal is not a substitute."""

    src = session.project.sheet(sheet_id)
    a = str(wire.get("from_tag") or "")
    b = str(wire.get("to_tag") or "")
    pa = str(wire.get("from_pin") or "")
    pb = str(wire.get("to_pin") or "")
    if not a or not b or not pa or not pb or a == b and pa == pb:
        return None
    try:
        comp_a = src.component(a)
        src.component(b)
    except KeyError:
        return None
    if a == b and not _pins_internally_jumpered(session, comp_a, pa, pb):
        return None
    # A terminal can land on more than one conductor. Only the same pin pair is a duplicate.
    edge = tuple(sorted(((a, pa), (b, pb))))
    if edge in used or _pair_on_sheet(src, a, pa, b, pb):
        _stamp_existing_number(session, sheet_id, a, pa, b, pb, str(wire.get("number") or ""))
        wire["duplicate_end"] = True
        return None
    try:
        _import_wire(
            session,
            f"{a}.{pa}",
            f"{b}.{pb}",
            net=str(wire.get("number") or ""),
            number=str(wire.get("number") or ""),
            sheet_id=sheet_id,
        )
    except Exception:  # noqa: BLE001
        return None
    used.add(edge)
    return (f"{a}.{pa}", f"{b}.{pb}")


def _connect(
    session: Session,
    sheet_id: str,
    wire: dict[str, Any],
    used: set[tuple[str, str]],
) -> tuple[str, str] | None:
    src = session.project.sheet(sheet_id)
    a = str(wire.get("from_tag") or "")
    b = str(wire.get("to_tag") or "")
    if not a or not b or a == b:
        return None
    try:
        comp_a = src.component(a)
        comp_b = src.component(b)
    except KeyError:
        return None
    pins_a = _pins(session, comp_a)
    pins_b = _pins(session, comp_b)
    preferred = []
    if wire.get("from_pin") and wire.get("to_pin"):
        preferred.append((str(wire["from_pin"]), str(wire["to_pin"])))
    side_a = _facing(comp_a.x, comp_a.y, comp_b.x, comp_b.y)
    side_b = _facing(comp_b.x, comp_b.y, comp_a.x, comp_a.y)
    ordered_a = _order_pins(session, comp_a, pins_a, side_a)
    ordered_b = _order_pins(session, comp_b, pins_b, side_b)
    for pa in ordered_a:
        for pb in ordered_b:
            preferred.append((pa, pb))
    for pa, pb in preferred:
        key_a = (a, pa)
        key_b = (b, pb)
        if key_a in used or key_b in used:
            continue
        try:
            _import_wire(
                session,
                f"{a}.{pa}",
                f"{b}.{pb}",
                net=str(wire.get("number") or ""),
                number=str(wire.get("number") or ""),
                sheet_id=sheet_id,
            )
        except Exception:  # noqa: BLE001
            continue
        used.add(key_a)
        used.add(key_b)
        return (f"{a}.{pa}", f"{b}.{pb}")
    return None


def _pins(session: Session, comp: Any) -> list[str]:
    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        return []
    return [pin.id for pin in device.pins]


def _order_pins(session: Session, comp: Any, pins: list[str], side: str) -> list[str]:
    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        return pins
    by_id = {pin.id: pin for pin in device.pins}
    facing = [pin for pin in pins if by_id.get(pin) and by_id[pin].side == side]
    rest = [pin for pin in pins if pin not in facing]
    return facing + rest


def _facing(ax: float, ay: float, bx: float, by: float) -> str:
    if abs(bx - ax) >= abs(by - ay):
        return "right" if bx >= ax else "left"
    return "bottom" if by >= ay else "top"


def _apply_photo_targets(
    session: Session,
    stitched: dict[str, Any],
    targets: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    from panelcad.library.panel import LayoutNotReadyError

    if not targets:
        return
    layout = next((sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"), None)
    created = False
    if layout is None:
        layout = session.add_sheet("photo-layout", title="Photo layout", kind="panel_layout")
        created = True
    existing = {comp.id for comp in layout.components}
    width = layout.page.dimensions()[0]
    height = layout.page.dimensions()[1]
    for target in targets:
        tag = str(target.get("tag") or "")
        if not tag or tag in existing:
            continue
        source = _find_component(session, tag)
        if source is None:
            continue
        x = 20.0 + float(target.get("nx") or 0) * max(40.0, width - 60.0)
        y = 20.0 + float(target.get("ny") or 0) * max(40.0, height - 80.0)
        try:
            device = session.catalog.get(source.library_id)
        except KeyError:
            report["skipped"].append({"tag": tag, "reason": "no_symbol", "sheet": layout.id})
            continue
        block = _unready_layout_reason(session, source.library_id)
        if block:
            _hold_unready_layout(session, layout.id, tag, source.library_id, x, y, report, block)
            continue
        try:
            session.place(
                source.library_id,
                ref=tag,
                x=x,
                y=y,
                sheet_id=layout.id,
                properties={
                    "print_import_source": "photo",
                    "print_import_confidence": target.get("confidence") or 0,
                },
                snap=False,
            )
        except LayoutNotReadyError as exc:
            _hold_unready_layout(session, layout.id, tag, source.library_id, x, y, report, str(exc))
            continue
        # Footprint size comes from the library. The photo only chose the spot.
        _ = device.panel.width_mm
        report["photos"].append({"tag": tag, "sheet": layout.id, "created_sheet": created})
        existing.add(tag)


def _sheet_is_layout(session: Session, sheet_id: str) -> bool:
    try:
        return session.project.sheet(sheet_id).kind in LAYOUT_KINDS
    except KeyError:
        return False


def _unready_layout_reason(session: Session, library_id: str) -> str:
    """Why this catalog row must not be placed on a layout. Empty when it may."""

    from panelcad.library.panel import is_layout_ready, layout_ready_gap, occupies_panel_layout

    try:
        device = session.catalog.get(library_id)
    except KeyError:
        return "no_symbol"
    if not occupies_panel_layout(device, None):
        return ""
    if is_layout_ready(device, None, parts=getattr(session, "parts", None)):
        return ""
    return layout_ready_gap(device, None, parts=getattr(session, "parts", None)) or "layout_not_ready"


def _hold_unready_layout(
    session: Session,
    sheet_id: str,
    tag: str,
    library_id: str,
    x: float,
    y: float,
    report: dict[str, Any],
    detail: str,
) -> None:
    """A layout part with no real catalog size stays visible.

    Reconstruct mode draws the INFO NEEDED mark. Every other import lists the
    part in the review. Neither path drops it quietly or aborts the import.
    """

    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID, reconstruct_mode

    if reconstruct_mode(session):
        try:
            session.place(
                NEEDS_INFO_LIBRARY_ID,
                ref=tag,
                x=x,
                y=y,
                sheet_id=sheet_id,
                properties={
                    "reconstruct_needs_info": "yes",
                    "description": "INFO NEEDED",
                    "label_text": tag,
                    "needs_missing": "catalog size",
                    "print_import_library": library_id,
                },
                snap=False,
            )
        except Exception as exc:  # noqa: BLE001 — the review row is the record
            report["skipped"].append(
                {
                    "tag": tag,
                    "reason": "needs_info",
                    "detail": str(exc),
                    "sheet": sheet_id,
                    "library_id": library_id,
                }
            )
            return
        report["skipped"].append(
            {
                "tag": tag,
                "reason": "needs_info",
                "detail": detail,
                "sheet": sheet_id,
                "library_id": library_id,
            }
        )
        return
    report["skipped"].append(
        {
            "tag": tag,
            "reason": "layout_not_ready",
            "detail": detail,
            "sheet": sheet_id,
            "library_id": library_id,
        }
    )


_ANCHOR_REASONS = {"needs_info", "tag_unparsed", "no_symbol"}


def _layout_furniture_tag(tag: str) -> bool:
    """Lowercase rail and duct ids printed on a layout. R1 and HMI stay part tags."""

    text = str(tag or "").strip()
    if text != text.lower():
        return False
    if re.fullmatch(r"r\d{1,3}", text):
        return True
    return bool(re.fullmatch(r"wt-[a-z0-9]+", text))


def _keep_read_needs_info(
    session: Session,
    sheet_id: str,
    tag: str,
    device: dict[str, Any],
    report: dict[str, Any],
    detail: str,
) -> None:
    """A read part with no library symbol stays on the sheet as a needs-info mark.

    It is flagged, it has no millimetres, and it stays off the BOM and the quote.
    A page of only these parts is still a project the review can open.
    """

    report["skipped"].append(
        {
            "tag": tag,
            "reason": "needs_info",
            "sheet": sheet_id,
            "detail": detail,
        }
    )
    from panelcad.importer.read import component_ref
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    ref = component_ref(tag)
    try:
        comp = session.project.sheet(sheet_id).component(ref)
    except KeyError:
        comp = _place_needs_info_anchor(session, sheet_id, tag, ref, device)
    if comp is None:
        return
    if str(comp.library_id or "") != NEEDS_INFO_LIBRARY_ID:
        return
    if any(str(row.get("tag") or "") == comp.id and str(row.get("sheet") or "") == sheet_id for row in report["placed"]):
        return
    report["placed"].append(
        {
            "tag": comp.id,
            "library_id": comp.library_id,
            "sheet": sheet_id,
            "needs_info": True,
            "guess": False,
        }
    )


def _anchor_needs_info_ends(
    session: Session,
    sheet_id: str,
    sheet: dict[str, Any],
    wires: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    """Give a needs-info device a pin so the wire is kept.

    The device stays off the BOM and the quote. The mark is already on the
    sheet when the tag was read. This only adds the pin the wire named.
    """

    from panelcad.importer.read import component_ref
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    skipped = {
        str(row.get("tag") or "").strip()
        for row in report.get("skipped") or []
        if str(row.get("sheet") or "") == sheet_id and str(row.get("reason") or "") in _ANCHOR_REASONS
    }
    devices = {
        str(device.get("tag") or "").strip(): device
        for device in (sheet.get("devices") or [])
        if isinstance(device, dict) and str(device.get("tag") or "").strip()
    }
    drawn = session.project.sheet(sheet_id)
    for wire in wires:
        for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
            tag = str(wire.get(tag_key) or "").strip()
            pin = str(wire.get(pin_key) or "").strip()
            if not tag or not pin or tag not in skipped:
                continue
            ref = component_ref(tag)
            try:
                comp = drawn.component(ref)
            except KeyError:
                comp = _place_needs_info_anchor(session, sheet_id, tag, ref, devices.get(tag) or {})
            if comp is None:
                continue
            if str(comp.library_id or "") == NEEDS_INFO_LIBRARY_ID:
                _remember_needs_info_pin(comp, pin)
            wire[tag_key] = comp.id


def _place_needs_info_anchor(
    session: Session,
    sheet_id: str,
    tag: str,
    ref: str,
    device: dict[str, Any],
) -> Any | None:
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    try:
        x = float(device.get("x") or 40.0)
        y = float(device.get("y") or 40.0)
    except (TypeError, ValueError):
        x, y = 40.0, 40.0
    try:
        return session.place(
            NEEDS_INFO_LIBRARY_ID,
            ref=ref,
            x=x,
            y=y,
            sheet_id=sheet_id,
            properties={
                "reconstruct_needs_info": "yes",
                "description": "INFO NEEDED",
                "label_text": tag,
                "needs_missing": "symbol",
                "bom": "no",
                "quote": "no",
                "in_panel": "no",
                "incabinet": "no",
                "print_import_anchor": "yes",
                "print_import_needs_info": "yes",
                "print_tag": tag,
            },
            snap=False,
            allow_needs_info=True,
        )
    except Exception:  # noqa: BLE001 — the wire stays unresolved when the anchor cannot be placed
        return None


def _remember_needs_info_pin(comp: Any, pin: str) -> None:
    name = str(pin or "").strip()
    if not name:
        return
    raw = str((comp.properties or {}).get("print_import_pins") or "")
    pins = [part.strip() for part in raw.split(",") if part.strip()]
    if name not in pins:
        pins.append(name)
    comp.properties["print_import_pins"] = ",".join(pins)


def _bus_label(wire: dict[str, Any]) -> str:
    from panelcad.importer.read import BUS_WIRE_RE

    bus = str(wire.get("bus") or "").strip().upper()
    number = str(wire.get("number") or "").strip().upper()
    if bus and BUS_WIRE_RE.match(bus):
        return bus
    if BUS_WIRE_RE.match(number):
        return number
    return ""


def _fully_pinned(wire: dict[str, Any]) -> bool:
    return bool(wire.get("from_pin") and wire.get("to_pin") and wire.get("from_tag") and wire.get("to_tag"))


def _geometry_library(device: dict[str, Any]) -> str:
    """Library id the ink, the model, or a person named. A prefix guess is not one."""

    incoming = str(device.get("library_id") or "").strip()
    if not incoming or device.get("guess"):
        return ""
    return incoming


def _library_id_is_ground(library_id: str) -> bool:
    lib = str(library_id or "").lower()
    return "terminals.pe" in lib or ".pe_" in lib or lib.endswith(".pe")


def _family_or_unknown(
    session: Session,
    tag: str,
    library_id: str,
    resolved: dict[str, Any],
    device: dict[str, Any],
    standard: str,
    guessed: bool,
) -> tuple[str, bool, bool]:
    """Keep a matched symbol. A wrong guess falls back to the generic family symbol.

    The pinless unknown box is only for a tag with no family. A geometry match
    is never replaced.
    """

    from panelcad.importer.symbols import (
        _classes_agree,
        _specific_rating,
        _symbol_only,
        generic_family_symbol,
        is_ground_symbol,
        normalize_symbol_word,
        shape_class,
        supported_tag_family,
        tag_is_ground,
    )

    _ = resolved
    part = session.catalog.devices.get(library_id) if library_id else None
    confirmed = bool(library_id) and _geometry_library(device) == library_id and not device.get("guess")
    if confirmed:
        return library_id, guessed, False
    asked = normalize_symbol_word(str(device.get("type") or device.get("symbol") or ""))
    got = shape_class(part) if part is not None else ""
    ground = bool(part is not None and is_ground_symbol(part) and not tag_is_ground(tag))
    class_clash = bool(asked and got and not _classes_agree(asked, got))
    generic = ""
    if part is not None and not _symbol_only(part) and _specific_rating(part):
        generic = generic_family_symbol(
            session.catalog,
            tag=tag,
            library_id=library_id,
            type_text=asked or got,
            standard=standard,
        )
        if generic == library_id:
            generic = ""
    if ground or class_clash or generic:
        if not generic:
            generic = generic_family_symbol(
                session.catalog,
                tag=tag,
                library_id="" if ground else library_id,
                type_text=asked or ("" if ground else got),
                standard=standard,
            )
        if generic and generic != library_id:
            return generic, True, False
        if ground or class_clash:
            if supported_tag_family(tag) or asked:
                if part is not None and part.pins and not ground:
                    return library_id, guessed, False
            return "", True, True
    if library_id:
        return library_id, guessed, False
    if supported_tag_family(tag) or asked:
        generic = generic_family_symbol(
            session.catalog,
            tag=tag,
            library_id="",
            type_text=asked or supported_tag_family(tag),
            standard=standard,
        )
        if generic:
            return generic, True, False
        # The family is known, but there is no symbol-only stand-in. Leave it
        # off the sheet instead of a pinless box.
        return "", guessed, False
    return "", True, True


def _place_unknown_device(
    session: Session,
    sheet_id: str,
    tag: str,
    device: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Needs-info mark for a symbol the library did not match. Off the BOM and the quote."""

    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    try:
        x = float(device.get("x") or 40.0)
        y = float(device.get("y") or 40.0)
    except (TypeError, ValueError):
        x, y = 40.0, 40.0
    props = {
        "reconstruct_needs_info": "yes",
        "description": "unknown device",
        "label_text": tag,
        "needs_missing": "symbol",
        "bom": "no",
        "in_panel": "no",
        "incabinet": "no",
        "print_import_anchor": "yes",
        "print_tag": tag,
    }
    crop = str(device.get("crop") or "").strip()
    if crop:
        props["print_import_ink"] = crop
    try:
        comp = session.place(
            NEEDS_INFO_LIBRARY_ID,
            ref=tag,
            x=x,
            y=y,
            sheet_id=sheet_id,
            properties=props,
            snap=False,
            allow_needs_info=True,
        )
        placed_id = comp.id
        placed_lib = comp.library_id
    except Exception as exc:  # noqa: BLE001 — the review row still records the miss
        if "already exists" not in str(exc).lower():
            report["skipped"].append(
                {"tag": tag, "reason": "needs_info", "sheet": sheet_id, "detail": "unknown device"}
            )
            return
        placed_id = tag
        placed_lib = NEEDS_INFO_LIBRARY_ID
    report["placed"].append(
        {
            "tag": placed_id,
            "library_id": placed_lib,
            "sheet": sheet_id,
            "guess": True,
            "needs_info": True,
        }
    )
    report["skipped"].append(
        {
            "tag": tag,
            "reason": "needs_info",
            "sheet": sheet_id,
            "detail": "NOT FOR BUILD. Unknown device. No millimetres. Kept off the BOM, the quote, and cutouts.",
        }
    )


def _library_confirmed(device: dict[str, Any]) -> bool:
    """True when a person or a catalog match chose this library id, not the vision model."""

    if device.get("catalog_matched"):
        return True
    return str(device.get("source") or "").strip().lower() in {"user", "library"}


def _import_mode(sheet: dict[str, Any], session: Session) -> str:
    return _library_mode(_sheet_import_standard(sheet, session))


def _sheet_import_standard(sheet: dict[str, Any], session: Session) -> str:
    """Page standard for this import. Unknown and mixed stay unknown."""

    if "standard" in sheet:
        raw = str(sheet.get("standard") or "")
    else:
        raw = str(session.project.title_block.standard or "")
    return _library_mode(raw) and {
        "nema": "NEMA/ANSI",
        "iec": "IEC",
        "unknown": "mixed" if str(raw).strip().lower() == "mixed" else "",
    }[_library_mode(raw)]


def _library_mode(raw: str) -> str:
    from panelcad.library.catalog import normalize_standard

    token = str(raw or "").strip().lower()
    if token in {"", "mixed", "unknown"}:
        return "unknown"
    norm = normalize_standard(raw, default="")
    if norm == "NEMA/ANSI":
        return "nema"
    if norm == "IEC":
        return "iec"
    return "unknown"


def _named_pin_missing(session: Session, sheet_id: str, wire: dict[str, Any]) -> bool:
    """A pin the placed symbol does not have stays needs-info. It is not moved."""

    sheet = session.project.sheet(sheet_id)
    for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
        tag = str(wire.get(tag_key) or "").strip()
        pin = str(wire.get(pin_key) or "").strip()
        if not tag or not pin:
            continue
        try:
            comp = sheet.component(tag)
            part = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        if "needs_info" in str(comp.library_id):
            continue
        names = [str(item.id) for item in part.pins]
        names.extend(str(item.id) for item in getattr(part, "ports", ()) or ())
        if not any(name.upper() == pin.upper() for name in names):
            return True
    return False


def _rejected_self_wire(session: Session, sheet_id: str, wire: dict[str, Any]) -> bool:
    """A wire from a device back to itself stays needs-info unless the pins are jumpered."""

    tag_a = str(wire.get("from_tag") or "").strip()
    tag_b = str(wire.get("to_tag") or "").strip()
    if not tag_a or tag_a != tag_b:
        return False
    pin_a = str(wire.get("from_pin") or "").strip()
    pin_b = str(wire.get("to_pin") or "").strip()
    if not pin_a or not pin_b or pin_a == pin_b:
        return True
    try:
        comp = session.project.sheet(sheet_id).component(tag_a)
    except KeyError:
        return True
    return not _pins_internally_jumpered(session, comp, pin_a, pin_b)


def _pins_internally_jumpered(session: Session, comp: Any, pin_a: str, pin_b: str) -> bool:
    try:
        part = session.catalog.get(comp.library_id)
    except KeyError:
        return False
    groups = {}
    for pin in part.pins:
        group = str(getattr(pin, "jumper", "") or "").strip()
        if group:
            groups[str(pin.id)] = group
    left = groups.get(pin_a) or groups.get(pin_a.upper())
    right = groups.get(pin_b) or groups.get(pin_b.upper())
    if left is None or right is None:
        for name, group in groups.items():
            if name.upper() == pin_a.upper():
                left = group
            if name.upper() == pin_b.upper():
                right = group
    return bool(left and left == right)


def _pair_on_sheet(sheet: Any, tag_a: str, pin_a: str, tag_b: str, pin_b: str) -> bool:
    wanted = {f"{tag_a}.{pin_a}".upper(), f"{tag_b}.{pin_b}".upper()}
    for item in sheet.wires:
        if {str(item.frm).upper(), str(item.to).upper()} == wanted:
            return True
    return False


def _rung_occurrences(
    session: Session,
    sheet_id: str,
    extracted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every extracted box on the rung, placed or not.

    A contact, a needs-info part, or an unreadable symbol still occupies its
    gap. Only a placed device whose tag is one symbol can be a wire end.
    """

    from panelcad.importer.symbols import normalize_symbol_word, shape_class
    from panelcad.importer.tile import _symbol_key, device_tag_key

    placed = _indexed_devices(session, sheet_id)
    # Shapes of one tag at one station. A coil on another rung is a second
    # occurrence, not a second reading of this box. Two shapes on the same
    # box are ambiguous, and that box is not a wire end.
    stations: dict[str, list[tuple[float, float, str]]] = {}

    def _add_shape(tag: str, x: float, y: float, shape: str) -> None:
        if tag and shape:
            stations.setdefault(tag, []).append((x, y, shape))

    for device in extracted:
        if isinstance(device, dict) and not device.get("secondary"):
            try:
                dx = float(device.get("x"))
                dy = float(device.get("y"))
            except (TypeError, ValueError):
                continue
            _add_shape(device_tag_key(str(device.get("tag") or "")), dx, dy, normalize_symbol_word(_symbol_key(device)))
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if _component_matches_extracted(comp, extracted):
                continue
            try:
                part = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            _add_shape(
                device_tag_key(str(comp.id)),
                float(comp.x),
                float(comp.y),
                normalize_symbol_word(shape_class(part)),
            )

    def _ambiguous(tag: str, x: float, y: float) -> bool:
        found = stations.get(tag, [])
        here = {shape for sx, sy, shape in found if math.hypot(sx - x, sy - y) <= 15.0}
        if len(here) > 1:
            return True
        # A coil and a contact are two symbols of one tag, even on another sheet.
        # The box still occupies its gap. It is not a wire end.
        return len({shape for _sx, _sy, shape in found}) > 1
    rows: list[dict[str, Any]] = []
    for device in extracted:
        # A second printing of the same name still occupies its box. Skipping it
        # would let a rung number jump a low-confidence contact. Its shape is
        # not counted above, so the placed occurrence stays a wire end.
        if not isinstance(device, dict) or device.get("endpoint"):
            continue
        try:
            x = float(device.get("x"))
            y = float(device.get("y"))
        except (TypeError, ValueError):
            continue
        tag = str(device.get("tag") or "")
        key = device_tag_key(tag)
        match = _placed_at(placed, key, x, y)
        blocked = bool(
            device.get("unreadable") or device.get("secondary") or _ambiguous(key, x, y) or match is None
        )
        row = {
            "tag": tag or key,
            "x": x,
            "y": y,
            "w": float(device.get("w") or 0),
            "h": float(device.get("h") or 0),
            "connectable": not blocked,
            "secondary": bool(device.get("secondary")),
            "pin_xy": dict(match.get("pin_xy") or {}) if match else {},
            "pins": set(match.get("pins") or ()) if match else set(),
        }
        rows.append(row)
    if rows:
        # A second copy of a tag on the same rung is the same part. It must not
        # sit in the gap. A low-confidence box on its own rung still blocks.
        tol_y = _row_tolerance(rows, "y")
        tol_x = _row_tolerance(rows, "x")
        # Same rung means close on the cross axis, not merely inside a wide cluster.
        kept_rows = []
        for row in rows:
            if row.get("secondary") and any(
                other["tag"] == row["tag"]
                and other.get("connectable")
                and (
                    abs(float(other["y"]) - float(row["y"])) <= tol_y
                    or abs(float(other["x"]) - float(row["x"])) <= tol_x
                )
                for other in rows
            ):
                continue
            kept_rows.append(row)
        return kept_rows
    for device in placed:
        rows.append(
            {
                **device,
                "connectable": not _ambiguous(
                    device_tag_key(str(device.get("tag") or "")),
                    float(device.get("x") or 0),
                    float(device.get("y") or 0),
                ),
            }
        )
    return rows


def _component_matches_extracted(comp: Any, extracted: list[dict[str, Any]]) -> bool:
    """True when this placed part is one of the boxes already counted on the sheet."""

    from panelcad.importer.tile import device_tag_key

    key = device_tag_key(str(getattr(comp, "id", "") or ""))
    if not key:
        return False
    for device in extracted:
        if not isinstance(device, dict):
            continue
        if device_tag_key(str(device.get("tag") or "")) != key:
            continue
        try:
            dist = math.hypot(float(comp.x) - float(device.get("x") or 0), float(comp.y) - float(device.get("y") or 0))
        except (TypeError, ValueError):
            continue
        if dist <= 15.0:
            return True
    return False


def _placed_at(placed: list[dict[str, Any]], tag: str, x: float, y: float) -> dict[str, Any] | None:
    from panelcad.importer.tile import device_tag_key

    best = None
    best_d = 15.0
    for device in placed:
        if device_tag_key(str(device.get("tag") or "")) != tag:
            continue
        dist = math.hypot(float(device.get("x") or 0) - x, float(device.get("y") or 0) - y)
        if dist <= best_d:
            best_d = dist
            best = device
    return best


def complete_numbered_rungs(wires: list[dict[str, Any]], devices: list[dict[str, Any]]) -> None:
    """Join a wire number that sits in one open gap between two placed devices.

    The gap is measured on every extracted box, including ones that were not
    placed. Anything between the two neighbors leaves the number needs-info.
    """

    rungs = _recognized_rungs(devices)
    if not rungs:
        return
    proposals: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []
    uncertain: list[dict[str, Any]] = []
    for wire in wires:
        if (
            wire.get("rejected")
            or wire.get("unreadable")
            or wire.get("label_unmatched")
            or _fully_pinned(wire)
            or _bus_label(wire)
        ):
            continue
        number = str(wire.get("number") or "").strip()
        point = _wire_xy(wire)
        if not number or point is None:
            continue
        rung = _rung_at_point(point, rungs)
        if rung is None:
            continue
        along, group, _tol = rung
        pair = _rung_gap(point, group, along)
        if pair is None:
            if _between_occurrences(point, group, along):
                uncertain.append(wire)
            continue
        left, right = pair
        if not left.get("connectable") or not right.get("connectable"):
            uncertain.append(wire)
            continue
        named = {str(wire.get("from_tag") or "").strip(), str(wire.get("to_tag") or "").strip()}
        named.discard("")
        if named and not named <= {left["tag"], right["tag"]}:
            continue
        proposals.append((wire, left, right, along))
    for wire in uncertain:
        wire["rung_uncertain"] = True
        wire["needs_review"] = True
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]]] = {}
    for item in proposals:
        key = tuple(sorted((item[1]["tag"], item[2]["tag"])))
        groups.setdefault(key, []).append(item)
    for items in groups.values():
        numbers = {str(item[0].get("number") or "").strip().upper() for item in items}
        if len(numbers) != 1:
            for wire, _left, _right, _along in items:
                wire["rung_uncertain"] = True
                wire["needs_review"] = True
            continue
        wire, left, right, along = items[0]
        from_pin = _gap_pin(wire, left, right, along, "from_pin")
        to_pin = _gap_pin(wire, right, left, along, "to_pin")
        if not from_pin or not to_pin:
            for pending, _left, _right, _along in items:
                pending["rung_uncertain"] = True
                pending["needs_review"] = True
            continue
        for pending, _left, _right, _along in items:
            pending["from_tag"] = left["tag"]
            pending["from_pin"] = from_pin
            pending["to_tag"] = right["tag"]
            pending["to_pin"] = to_pin
            pending["ambiguous"] = False
            pending["stub"] = False
            pending["attached"] = True
            pending["rung_uncertain"] = False


def _recognized_rungs(
    devices: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]], float]]:
    """Horizontal and vertical runs of at least two devices. The third value is the row tolerance."""

    rungs: list[tuple[str, list[dict[str, Any]], float]] = []
    # Rung spacing comes from the placed symbols. A second copy of a tag, or a
    # low-confidence box, must not split one rung into two.
    anchors = [device for device in devices if device.get("connectable", True)]
    y_tol = _row_tolerance(anchors or devices, "y")
    for group in _cluster_devices(devices, "y", y_tol):
        if len(group) >= 2 and _axis_span(group, "x") >= _axis_span(group, "y"):
            rungs.append(("x", group, y_tol))
    x_tol = _row_tolerance(anchors or devices, "x")
    for group in _cluster_devices(devices, "x", x_tol):
        if len(group) >= 2 and _axis_span(group, "y") > _axis_span(group, "x"):
            rungs.append(("y", group, x_tol))
    return rungs


def _row_tolerance(devices: list[dict[str, Any]], cross: str) -> float:
    """Separate rung rows using this page's own gaps and symbol size."""

    spans = [_symbol_cross_span(device, cross) for device in devices]
    spans = [span for span in spans if span > 0]
    floor = (sorted(spans)[len(spans) // 2] * 0.5) if spans else 1.0
    coords = sorted(float(device.get(cross) or 0) for device in devices)
    gaps = [right - left for left, right in zip(coords, coords[1:]) if right - left >= 0.5]
    if len(gaps) < 2:
        return max(floor, 1.0)
    ranked = sorted(gaps)
    split = 0
    best = 1.0
    for index in range(len(ranked) - 1):
        small = ranked[index]
        large = ranked[index + 1]
        if small <= 0:
            continue
        ratio = large / small
        if ratio > best:
            best = ratio
            split = index
    if best >= 2.0:
        return (ranked[split] + ranked[split + 1]) / 2.0
    return max(floor, ranked[len(ranked) // 2] * 0.35)


def _symbol_cross_span(device: dict[str, Any], cross: str) -> float:
    origin = float(device.get(cross) or 0)
    coords = [origin]
    for px, py in (device.get("pin_xy") or {}).values():
        coords.append(py if cross == "y" else px)
    return max(coords) - min(coords)


def _cluster_devices(devices: list[dict[str, Any]], cross: str, tol: float) -> list[list[dict[str, Any]]]:
    ordered = sorted(devices, key=lambda device: float(device.get(cross) or 0))
    groups: list[list[dict[str, Any]]] = []
    for device in ordered:
        if not groups:
            groups.append([device])
            continue
        anchor = sorted(float(item.get(cross) or 0) for item in groups[-1])[len(groups[-1]) // 2]
        if abs(float(device.get(cross) or 0) - anchor) <= tol:
            groups[-1].append(device)
        else:
            groups.append([device])
    return groups


def _axis_span(group: list[dict[str, Any]], axis: str) -> float:
    values = [float(device.get(axis) or 0) for device in group]
    return max(values) - min(values) if values else 0.0


def _rung_at_point(
    point: tuple[float, float],
    rungs: list[tuple[str, list[dict[str, Any]], float]],
) -> tuple[str, list[dict[str, Any]], float] | None:
    best: tuple[float, tuple[str, list[dict[str, Any]], float]] | None = None
    for along, group, tol in rungs:
        cross = "y" if along == "x" else "x"
        value = point[1] if cross == "y" else point[0]
        coords = _cross_coords(group, cross)
        if not coords:
            continue
        if value < min(coords) - tol or value > max(coords) + tol:
            continue
        median = sorted(float(device.get(cross) or 0) for device in group)[len(group) // 2]
        distance = abs(value - median)
        if best is None or distance < best[0]:
            best = (distance, (along, group, tol))
    if best is None:
        return None
    return best[1]


def _cross_coords(group: list[dict[str, Any]], cross: str) -> list[float]:
    coords = [float(device.get(cross) or 0) for device in group]
    for device in group:
        for px, py in (device.get("pin_xy") or {}).values():
            coords.append(py if cross == "y" else px)
    return coords


def _between_occurrences(
    point: tuple[float, float],
    group: list[dict[str, Any]],
    along: str,
) -> bool:
    """True when the number sits somewhere along the rung, not off in open space."""

    value = point[0] if along == "x" else point[1]
    coords = [float(device.get(along) or 0) for device in group]
    if len(coords) < 2:
        return False
    return min(coords) < value < max(coords)


def _rung_gap(
    point: tuple[float, float],
    group: list[dict[str, Any]],
    along: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The two devices the number sits between, when that pair is one open gap."""

    value = point[0] if along == "x" else point[1]
    ordered = sorted(group, key=lambda device: float(device.get(along) or 0))
    left: dict[str, Any] | None = None
    right: dict[str, Any] | None = None
    for device in ordered:
        pos = float(device.get(along) or 0)
        if pos < value:
            left = device
        elif right is None:
            right = device
            break
    if left is None or right is None or left["tag"] == right["tag"]:
        return None
    left_end = float(left.get(along) or 0) + _along_reach(left, along)
    right_pos = float(right.get(along) or 0)
    if not (left_end < value < right_pos):
        return None
    if _same_station(ordered, left, along) or _same_station(ordered, right, along):
        return None
    return left, right


def _along_reach(device: dict[str, Any], along: str) -> float:
    origin = float(device.get(along) or 0)
    coords = [origin]
    for px, py in (device.get("pin_xy") or {}).values():
        coords.append(px if along == "x" else py)
    return max(0.0, max(coords) - origin)


def _same_station(ordered: list[dict[str, Any]], device: dict[str, Any], along: str) -> bool:
    pos = float(device.get(along) or 0)
    return sum(1 for other in ordered if abs(float(other.get(along) or 0) - pos) <= 1.0) > 1


def _gap_pin(
    wire: dict[str, Any],
    device: dict[str, Any],
    other: dict[str, Any],
    along: str,
    field: str,
) -> str:
    named = str(wire.get(field) or "").strip()
    if named:
        return named if _has_pin(device, named) else ""
    return _unique_end_pin(device, other, along) or ""


def _unique_end_pin(device: dict[str, Any], toward: dict[str, Any], along: str) -> str | None:
    """The single pin on the side that faces ``toward``. Several pins are not a guess."""

    forward = 1.0 if float(toward.get(along) or 0) >= float(device.get(along) or 0) else -1.0
    scored: list[tuple[float, str]] = []
    for pin, (px, py) in (device.get("pin_xy") or {}).items():
        coord = px if along == "x" else py
        scored.append((coord * forward, str(pin)))
    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0][0]
    tied = [pin for coord, pin in scored if best - coord <= 0.75]
    if len(tied) != 1:
        return None
    return tied[0]


def _phase_pin(label: str, labels: list[str]) -> str:
    """Map the last letter of a bus family onto L1, L2, L3 in letter order.

    9B16B/C/D become L1/L2/L3. Any other stem with a trailing column letter
    uses the same order. A Square D starter is not special.
    """

    from panelcad.importer.read import BUS_WIRE_RE

    stem = label[:-1]
    letters = sorted({item[-1] for item in labels if item[:-1] == stem and BUS_WIRE_RE.match(item)})
    if not letters or label[-1] not in letters:
        return ""
    names = ("L1", "L2", "L3")
    index = letters.index(label[-1])
    if index >= len(names):
        return ""
    return names[index]


def _tie_bus(
    session: Session,
    sheet_id: str,
    label: str,
    labels: list[str],
    report: dict[str, Any],
    used: set[tuple[tuple[str, str], tuple[str, str]]],
    *,
    distance_mm: Any = None,
    confidence: Any = None,
    source: dict[str, Any] | None = None,
) -> None:
    """Join every device that owns this bus phase. Three contactors make three ties."""

    def _park() -> None:
        row: dict[str, Any] = {"number": label, "reason": "bus", "sheet": sheet_id}
        if distance_mm is not None:
            row["distance_mm"] = distance_mm
        if confidence is not None:
            row["confidence"] = confidence
        _remember_unresolved(report, source, row)

    pin = _phase_pin(label, labels)
    if not pin:
        _park()
        return
    sheet = session.project.sheet(sheet_id)
    owners = []
    for comp in sheet.components:
        try:
            part = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        if any(str(item.id) == pin for item in part.pins):
            owners.append(comp)
    if len(owners) < 2:
        _park()
        return
    xs = [float(comp.x) for comp in owners]
    ys = [float(comp.y) for comp in owners]
    if max(ys) - min(ys) >= max(xs) - min(xs):
        owners.sort(key=lambda comp: (float(comp.y), float(comp.x)))
    else:
        owners.sort(key=lambda comp: (float(comp.x), float(comp.y)))
    placed = False
    for index, left in enumerate(owners):
        for right in owners[index + 1 :]:
            pair = _connect_traced(
                session,
                sheet_id,
                {
                    "number": label,
                    "from_tag": left.id,
                    "from_pin": pin,
                    "to_tag": right.id,
                    "to_pin": pin,
                },
                used,
            )
            if pair is None:
                continue
            report["wires"].append({"number": label, "sheet": sheet_id, "from": pair[0], "to": pair[1]})
            placed = True
    if not placed:
        _park()


def complete_conductor_ends(session: Session, sheet_id: str, wires: list[dict[str, Any]]) -> None:
    """Fill a missing to_tag or from_tag from the next device on the rung that owns the pin.

    Load direction follows the symbol rotation. 270° sends the load to the right.
    A vertical starter (rotation 0) sends it downward. The printed tag is not changed.
    Both pins and no tags use the device nearest the label that owns ``from_pin``.
    Overload load pins 2/4/6 land on the motor leads T1/T2/T3.
    """

    devices = _indexed_devices(session, sheet_id)
    groups: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        if device["prefix"]:
            groups.setdefault(str(device["prefix"]), []).append(device)
    for wire in wires:
        if wire.get("rejected"):
            continue
        _fill_missing_end(wire, groups, devices)


def _load_order(group: list[dict[str, Any]]) -> tuple[str, int]:
    """Axis and sign toward the load. Positive sign is increasing millimetres."""

    rotations = {int(device.get("rotation") or 0) for device in group}
    if 270 in rotations:
        return "x", 1
    if 90 in rotations:
        return "x", -1
    if 180 in rotations and 0 not in rotations:
        return "y", -1
    xs = [float(device["x"]) for device in group]
    ys = [float(device["y"]) for device in group]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    if dx > dy + 15 and dy < 40:
        return "x", 1
    return "y", 1


# A label sitting on the conductor is within this of the pin-to-pin segment.
# The next rung is about 80 mm away, so 22 mm does not cross it.
LABEL_ATTACH_MM = 22.0
# The near pin of a one-ended row. The far end of that rung is still on this device.
_NEAR_PIN_MM = 40.0
_OL_LOAD_TO_MOTOR = {"2": "T1", "4": "T2", "6": "T3"}
_MOTOR_LEAD_TO_OL = {"T1": "2", "T2": "4", "T3": "6"}
_LEAD_NUMBER = re.compile(r"^(\d+)T([123])$")


def _fill_missing_end(
    wire: dict[str, Any],
    groups: dict[str, list[dict[str, Any]]],
    devices: list[dict[str, Any]],
) -> None:
    from_tag = str(wire.get("from_tag") or "").strip()
    to_tag = str(wire.get("to_tag") or "").strip()
    from_pin = str(wire.get("from_pin") or "").strip()
    to_pin = str(wire.get("to_pin") or "").strip()
    if (from_tag and to_tag) or not from_pin or not to_pin:
        return
    if not from_tag and not to_tag:
        _assign_near_tag(wire, devices)
        from_tag = str(wire.get("from_tag") or "").strip()
        from_pin = str(wire.get("from_pin") or "").strip()
        to_pin = str(wire.get("to_pin") or "").strip()
    known = from_tag or str(wire.get("to_tag") or "").strip()
    if not known:
        return
    match = re.match(r"(\d+)", known)
    if not match:
        return
    group = groups.get(match.group(1)) or []
    if len(group) < 2:
        return
    axis, sign = _load_order(group)
    ordered = sorted(group, key=lambda device: float(device[axis]) * sign)
    origin = next((device for device in ordered if device["tag"] == known), None)
    if origin is None:
        return
    index = ordered.index(origin)
    missing_to = bool(from_tag and not str(wire.get("to_tag") or "").strip())
    if missing_to:
        near_pin, far_pin = from_pin, to_pin
        wanted, field = to_pin, "to_tag"
    else:
        near_pin, far_pin = to_pin, from_pin
        wanted, field = from_pin, "from_tag"
    mapped = _mapped_partner(origin, near_pin, far_pin, ordered, index)
    if mapped:
        wire[field] = mapped
        return
    for device in list(ordered[index + 1 :]) + list(reversed(ordered[:index])):
        if _has_pin(device, wanted) and device["tag"] != known:
            wire[field] = device["tag"]
            return


def _indexed_devices(session: Session, sheet_id: str) -> list[dict[str, Any]]:
    """Placed symbols with world pin coordinates. A missing catalog row is skipped."""

    sheet = session.project.sheet(sheet_id)
    devices: list[dict[str, Any]] = []
    for comp in sheet.components:
        try:
            part = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        rotation = int(getattr(comp, "rotation", 0) or 0) % 360
        pin_xy: dict[str, tuple[float, float]] = {}
        for pin in part.pins:
            px, py = rotate_point(float(pin.x) * scale, float(pin.y) * scale, rotation)
            pin_xy[str(pin.id)] = (float(comp.x) + px, float(comp.y) + py)
        match = re.match(r"(\d+)", str(comp.id))
        devices.append(
            {
                "tag": str(comp.id),
                "x": float(comp.x),
                "y": float(comp.y),
                "rotation": rotation,
                "pins": set(pin_xy),
                "pin_xy": pin_xy,
                "prefix": match.group(1) if match else "",
                "lib": str(comp.library_id or "").lower(),
            }
        )
    return devices


def _has_pin(device: dict[str, Any], name: str) -> bool:
    wanted = str(name or "").strip().upper()
    if not wanted:
        return False
    return any(str(pin).upper() == wanted for pin in device.get("pins") or ())


def _pin_xy_of(device: dict[str, Any], name: str) -> tuple[float, float] | None:
    wanted = str(name or "").strip().upper()
    for pin, loc in (device.get("pin_xy") or {}).items():
        if str(pin).upper() == wanted:
            return loc
    return None


def _wire_xy(wire: dict[str, Any]) -> tuple[float, float] | None:
    try:
        return float(wire["x"]), float(wire["y"])
    except (KeyError, TypeError, ValueError):
        return None


def _device_is_overload(device: dict[str, Any]) -> bool:
    lib = str(device.get("lib") or "")
    return "overload" in lib and "ol_contact" not in lib


def _device_is_motor(device: dict[str, Any]) -> bool:
    lib = str(device.get("lib") or "")
    return "motor" in lib and "1ph" not in lib


def _nearest_pin(devices: list[dict[str, Any]], point: tuple[float, float], pin_id: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_d = _NEAR_PIN_MM
    px, py = point
    for device in devices:
        loc = _pin_xy_of(device, pin_id)
        if loc is None:
            continue
        dist = math.hypot(loc[0] - px, loc[1] - py)
        if dist <= best_d:
            best_d = dist
            best = device
    return best


def _assign_near_tag(wire: dict[str, Any], devices: list[dict[str, Any]]) -> None:
    """Name the device that owns ``from_pin`` when the row gave only the two pins.

    The label sits on the conductor. The nearer end is the device whose pin is
    within ``_NEAR_PIN_MM``. A row whose ``to_pin`` is the near one is swapped
    so ``from_tag`` is that device.
    """

    point = _wire_xy(wire)
    if point is None:
        return
    from_pin = str(wire.get("from_pin") or "").strip()
    to_pin = str(wire.get("to_pin") or "").strip()
    near = _nearest_pin(devices, point, from_pin) if from_pin else None
    if near is not None:
        wire["from_tag"] = near["tag"]
        return
    other = _nearest_pin(devices, point, to_pin) if to_pin else None
    if other is None:
        return
    wire["from_tag"] = other["tag"]
    wire["from_pin"] = to_pin
    wire["to_pin"] = from_pin


def _mapped_partner(
    origin: dict[str, Any],
    near_pin: str,
    far_pin: str,
    ordered: list[dict[str, Any]],
    index: int,
) -> str:
    """Overload load 2/4/6 lands on motor T1/T2/T3. The motor lead lands back on that element.

    Downstream is preferred from an overload. Upstream is preferred from a motor.
    A tag that was not placed is not invented.
    """

    near = str(near_pin or "").strip().upper()
    far = str(far_pin or "").strip().upper()
    if _device_is_overload(origin) and _OL_LOAD_TO_MOTOR.get(near) == far:
        order = list(ordered[index + 1 :]) + list(reversed(ordered[:index]))
        for device in order:
            if _device_is_motor(device) and _has_pin(device, far):
                return str(device["tag"])
    if _device_is_motor(origin) and _MOTOR_LEAD_TO_OL.get(near) == far:
        order = list(reversed(ordered[:index])) + list(ordered[index + 1 :])
        for device in order:
            if _device_is_overload(device) and _has_pin(device, far):
                return str(device["tag"])
    return ""


def _bind_loose_labels(session: Session, sheet_id: str, wires: list[dict[str, Any]]) -> None:
    """Attach a bbox-only wire number or bus to the conductor it sits on.

    A lead number ``10T1`` binds only to that starter's overload 2 – motor T1
    segment, and only when the box is within ``LABEL_ATTACH_MM``. A farther box
    is parked with the distance. It is not moved onto a closer phase.
    A bus label is measured the same way and still tied by its phase letter.
    The tie is not refused for distance: a label beside the line can sit a
    little off the pin.
    """

    devices = _indexed_devices(session, sheet_id)
    segments = _power_segments(devices)
    labels = [label for label in (_bus_label(wire) for wire in wires) if label]
    for wire in wires:
        if wire.get("rejected"):
            continue
        if _bus_label(wire):
            dist = _bus_distance(wire, devices, labels)
            if dist is not None:
                wire["label_distance_mm"] = round(dist, 2)
            continue
        if _fully_pinned(wire):
            continue
        number = str(wire.get("number") or "").strip().upper()
        match = _LEAD_NUMBER.match(number)
        if not match:
            continue
        point = _wire_xy(wire)
        if point is None:
            continue
        segment = _best_label_segment(segments, match.group(1), match.group(2))
        if segment is None:
            continue
        dist = _point_segment_mm(point[0], point[1], segment["a"], segment["b"])
        if dist <= LABEL_ATTACH_MM:
            wire["from_tag"] = segment["from_tag"]
            wire["from_pin"] = segment["from_pin"]
            wire["to_tag"] = segment["to_tag"]
            wire["to_pin"] = segment["to_pin"]
            wire["stub"] = False
            wire["attached"] = True
            wire["ambiguous"] = False
            wire["label_unmatched"] = False
            continue
        wire["label_distance_mm"] = round(dist, 2)
        wire["label_unmatched"] = True


def _power_segments(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overload load pin to motor lead, one segment per phase, for each starter group."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        if device.get("prefix"):
            groups.setdefault(str(device["prefix"]), []).append(device)
    phases = (("1", "2", "T1"), ("2", "4", "T2"), ("3", "6", "T3"))
    segments: list[dict[str, Any]] = []
    for prefix, group in groups.items():
        if len(group) < 2:
            continue
        axis, sign = _load_order(group)
        ordered = sorted(group, key=lambda device: float(device[axis]) * sign)
        for left, right in zip(ordered, ordered[1:]):
            overload, motor = left, right
            if _device_is_motor(left) and _device_is_overload(right):
                overload, motor = right, left
            if not (_device_is_overload(overload) and _device_is_motor(motor)):
                continue
            for digit, ol_pin, motor_pin in phases:
                start = _pin_xy_of(overload, ol_pin)
                end = _pin_xy_of(motor, motor_pin)
                if start is None or end is None:
                    continue
                segments.append(
                    {
                        "prefix": prefix,
                        "digit": digit,
                        "from_tag": overload["tag"],
                        "from_pin": ol_pin,
                        "to_tag": motor["tag"],
                        "to_pin": motor_pin,
                        "a": start,
                        "b": end,
                    }
                )
    return segments


def _best_label_segment(segments: list[dict[str, Any]], prefix: str, digit: str) -> dict[str, Any] | None:
    for segment in segments:
        if segment["prefix"] == prefix and segment["digit"] == digit:
            return segment
    return None


def _point_segment_mm(px: float, py: float, start: tuple[float, float], end: tuple[float, float]) -> float:
    """Millimetres from a point to a segment. A zero-length segment is the point distance."""

    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _bus_distance(wire: dict[str, Any], devices: list[dict[str, Any]], labels: list[str]) -> float | None:
    point = _wire_xy(wire)
    label = _bus_label(wire)
    if point is None or not label:
        return None
    pin = _phase_pin(label, labels)
    if not pin:
        return None
    points = [loc for device in devices if (loc := _pin_xy_of(device, pin)) is not None]
    if not points:
        return None
    if len(points) == 1:
        return math.hypot(point[0] - points[0][0], point[1] - points[0][1])
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    if max(ys) - min(ys) >= max(xs) - min(xs):
        points = sorted(points, key=lambda item: (item[1], item[0]))
    else:
        points = sorted(points, key=lambda item: (item[0], item[1]))
    return min(
        _point_segment_mm(point[0], point[1], points[index], points[index + 1]) for index in range(len(points) - 1)
    )


def _remember_unresolved(report: dict[str, Any], wire: dict[str, Any] | None, row: dict[str, Any]) -> None:
    """Keep the first printed place so Problems can zoom back to it."""

    if wire:
        point = _wire_xy(wire)
        if point is not None:
            row.setdefault("x", point[0])
            row.setdefault("y", point[1])
        crop = str(wire.get("crop") or "").strip()
        if crop:
            row.setdefault("crop", crop)
    report["unresolved_wires"].append(row)


def _owned_pin_ids(used: set[tuple[tuple[str, str], tuple[str, str]]], tag: str) -> set[str]:
    owned: set[str] = set()
    for edge in used:
        for item_tag, pin in edge:
            if item_tag == tag and pin:
                owned.add(pin)
    return owned


def _realize_provisional_pins(
    session: Session,
    sheet_id: str,
    wire: dict[str, Any],
    used: set[tuple[tuple[str, str], tuple[str, str]]] | None = None,
) -> bool:
    """Map a side name (left/right/top/bottom) onto the placed symbol's pin on that side."""

    ok = True
    for tag_key, pin_key, x_key, y_key in (
        ("from_tag", "from_pin", "from_x", "from_y"),
        ("to_tag", "to_pin", "to_x", "to_y"),
    ):
        pin = str(wire.get(pin_key) or "")
        if pin not in {"left", "right", "top", "bottom"}:
            continue
        tag = str(wire.get(tag_key) or "")
        # An unmatched symbol has no library terminals. The side the conductor
        # arrives on is the pin, and the wire is kept.
        try:
            comp = session.project.sheet(sheet_id).component(tag)
        except KeyError:
            comp = None
        if comp is not None and "needs_info" in str(comp.library_id or ""):
            # A coil's line terminal is A1 and its return is A2. An unmatched
            # symbol keeps that arrival: left and top are A1, right and bottom are A2.
            named = {"left": "A1", "right": "A2", "top": "A1", "bottom": "A2"}[pin]
            wire[pin_key] = named
            _remember_needs_info_pin(comp, named)
            continue
        mapped = _closest_side_pin(
            session,
            sheet_id,
            tag,
            pin,
            wire.get(x_key),
            wire.get(y_key),
            owned=_owned_pin_ids(used or set(), tag),
        )
        if not mapped:
            ok = False
            continue
        wire[pin_key] = mapped
    return ok


def _closest_side_pin(
    session: Session,
    sheet_id: str,
    tag: str,
    side: str,
    x: Any,
    y: Any,
    owned: set[str] | None = None,
) -> str:
    try:
        comp = session.project.sheet(sheet_id).component(tag)
        device = session.catalog.get(comp.library_id)
    except KeyError:
        return ""
    pins = list(device.pins)
    if x is None or y is None:
        chosen = choose_side_pin(pins, side, None, owned=owned)
        return "" if chosen is None else str(chosen.id)
    target = (float(x) - float(comp.x), float(y) - float(comp.y))
    chosen = choose_side_pin(pins, side, target, owned=owned)
    return "" if chosen is None else str(chosen.id)


def choose_side_pin(
    pins: list[Any],
    side: str,
    target: tuple[float, float] | None,
    owned: set[str] | None = None,
) -> Any | None:
    """The terminal on the side the conductor arrives on.

    A side with no terminal uses the pin on the conductor's line, when that
    pin is the only one there and no other conductor already owns it.
    """

    pool = [pin for pin in pins if str(getattr(pin, "side", "")) == side]
    if pool:
        if target is None:
            return pool[0]
        tx, ty = target
        return min(pool, key=lambda pin: (float(pin.x) - tx) ** 2 + (float(pin.y) - ty) ** 2)
    if target is None or not pins:
        return None
    tx, ty = target
    horizontal = side in {"left", "right"}

    def axis_gap(pin: Any) -> float:
        return abs((float(pin.y) if horizontal else float(pin.x)) - (ty if horizontal else tx))

    def along(pin: Any) -> float:
        return abs((float(pin.x) if horizontal else float(pin.y)) - (tx if horizontal else ty))

    on_axis = [pin for pin in pins if axis_gap(pin) <= 6.0]
    if owned:
        on_axis = [pin for pin in on_axis if str(pin.id) not in owned]
    if not on_axis:
        return None
    ranked = sorted(on_axis, key=lambda pin: (axis_gap(pin), along(pin)))
    if len(ranked) > 1 and axis_gap(ranked[1]) < axis_gap(ranked[0]) + 1.5 and along(ranked[1]) < along(ranked[0]) + 4.0:
        return None
    return ranked[0]


def _unconnected_label(wire: dict[str, Any], number: str, sheet_id: str) -> dict[str, Any]:
    """A number that was read and did not land on two terminals."""

    reason = "connect_by_hand" if wire.get("connect_by_hand") else "ambiguous"
    row: dict[str, Any] = {
        "number": number,
        "reason": reason,
        "sheet": sheet_id,
        "confidence": wire.get("confidence") or 0,
    }
    if wire.get("x") is not None and wire.get("y") is not None:
        row["x"] = wire.get("x")
        row["y"] = wire.get("y")
    return row


def _parked_label(wire: dict[str, Any], number: str, sheet_id: str, reason: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": number,
        "reason": reason,
        "sheet": sheet_id,
        "confidence": wire.get("confidence") or 0,
    }
    if wire.get("label_distance_mm") is not None:
        row["distance_mm"] = wire["label_distance_mm"]
    if wire.get("x") is not None and wire.get("y") is not None:
        row["x"] = wire.get("x")
        row["y"] = wire.get("y")
    return row


def _stamp_existing_number(
    session: Session,
    sheet_id: str,
    a: str,
    pa: str,
    b: str,
    pb: str,
    number: str,
) -> bool:
    """Keep one wire for a pin pair. A printed number replaces an automatic one.

    An empty incoming number leaves the wire that is already there. Two different
    locked numbers stay unresolved.
    """

    sheet = session.project.sheet(sheet_id)
    wanted = {f"{a}.{pa}".upper(), f"{b}.{pb}".upper()}
    incoming = str(number or "").strip()
    for item in sheet.wires:
        if {str(item.frm).upper(), str(item.to).upper()} != wanted:
            continue
        current = str(item.number or "").strip()
        if not incoming or current.upper() == incoming.upper():
            return True
        if not current or not item.number_locked:
            item.number = incoming
            item.number_locked = True
            if not item.net or str(item.net) == current:
                item.net = incoming
            return True
        return False
    return False


# A direct-mounted overload sits on the contactor's load pins. The vertical
# starter fixture leaves 26 mm between a 74 mm contactor and the next origin.
# The spread rung leaves 6 mm. A longer gap is a wire the drawing would show.
_DIRECT_MOUNT_MM = 30.0
_MOUNT_LATERAL_MM = 18.0
_IEC_OVERLOAD_LINE = {"T1": "1", "T2": "3", "T3": "5"}


def _connect_direct_mounts(
    session: Session,
    sheet_id: str,
    report: dict[str, Any],
    used: set[tuple[tuple[str, str], tuple[str, str]]],
) -> None:
    """Connect contactor T1–T3 to the overload line when no wire is drawn.

    The pair has to share a leading-digit group and face each other along the
    load direction. An overload further down the rung is left alone. A wire
    already in ``used`` is not drawn again.
    """

    import re

    sheet = session.project.sheet(sheet_id)
    groups: dict[str, list[dict[str, Any]]] = {}
    for comp in sheet.components:
        match = re.match(r"(\d+)", str(comp.id))
        if not match:
            continue
        try:
            part = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        pins = {str(pin.id): pin for pin in part.pins}
        row = {
            "comp": comp,
            "pins": pins,
            "lib": str(comp.library_id or "").lower(),
            "x": float(comp.x),
            "y": float(comp.y),
            "rotation": int(getattr(comp, "rotation", 0) or 0) % 360,
            "prefix": match.group(1),
        }
        groups.setdefault(match.group(1), []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        axis, sign = _load_order(group)
        cross = "y" if axis == "x" else "x"
        contactors = [row for row in group if _mount_contactor(row)]
        overloads = [row for row in group if _mount_overload(row)]
        for contactor in contactors:
            chosen: dict[str, Any] | None = None
            chosen_gap: float | None = None
            for overload in overloads:
                if abs(float(contactor[cross]) - float(overload[cross])) > _MOUNT_LATERAL_MM:
                    continue
                gap = _mount_gap(contactor, overload, axis, sign)
                if gap is None or gap < -2.0 or gap > _DIRECT_MOUNT_MM:
                    continue
                if chosen_gap is None or gap < chosen_gap:
                    chosen = overload
                    chosen_gap = gap
            if chosen is None:
                continue
            for phase in ("T1", "T2", "T3"):
                line = _overload_line_pin(chosen["pins"], phase)
                if phase not in contactor["pins"] or not line:
                    continue
                pair = _connect_traced(
                    session,
                    sheet_id,
                    {
                        "number": "",
                        "from_tag": contactor["comp"].id,
                        "from_pin": phase,
                        "to_tag": chosen["comp"].id,
                        "to_pin": line,
                    },
                    used,
                )
                if pair is None:
                    continue
                report["wires"].append({"number": "", "sheet": sheet_id, "from": pair[0], "to": pair[1]})


def _mount_contactor(row: dict[str, Any]) -> bool:
    lib = str(row.get("lib") or "")
    if "contactor" not in lib or "aux" in lib or "coil" in lib:
        return False
    pins = row.get("pins") or {}
    return all(phase in pins for phase in ("T1", "T2", "T3"))


def _mount_overload(row: dict[str, Any]) -> bool:
    lib = str(row.get("lib") or "")
    if "overload" not in lib or "ol_contact" in lib:
        return False
    pins = row.get("pins") or {}
    return any(_overload_line_pin(pins, phase) for phase in ("T1", "T2", "T3"))


def _overload_line_pin(pins: dict[str, Any], phase: str) -> str:
    if phase in pins:
        return phase
    mapped = _IEC_OVERLOAD_LINE.get(phase, "")
    if mapped and mapped in pins:
        return mapped
    return ""


def _mount_gap(contactor: dict[str, Any], overload: dict[str, Any], axis: str, sign: int) -> float | None:
    """Millimetres from the contactor load pins to the overload line pins."""

    load_at = _pin_axis(contactor, ("T1", "T2", "T3"), axis)
    line_names = tuple(_overload_line_pin(overload["pins"], phase) for phase in ("T1", "T2", "T3"))
    if any(not name for name in line_names):
        return None
    line_at = _pin_axis(overload, line_names, axis)
    if load_at is None or line_at is None:
        return None
    return (line_at - load_at) * sign


def _pin_axis(row: dict[str, Any], names: tuple[str, ...], axis: str) -> float | None:
    comp = row["comp"]
    scale = comp.visual_scale()
    rotation = int(getattr(comp, "rotation", 0) or 0)
    values: list[float] = []
    for name in names:
        pin = row["pins"].get(name)
        if pin is None:
            return None
        px, py = rotate_point(float(pin.x) * scale, float(pin.y) * scale, rotation)
        world_x = float(comp.x) + px
        world_y = float(comp.y) + py
        values.append(world_x if axis == "x" else world_y)
    if not values:
        return None
    return sum(values) / len(values)


def _find_component(session: Session, tag: str) -> Any | None:
    for sheet in session.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        try:
            return sheet.component(tag)
        except KeyError:
            continue
    return None


def _stamp_properties(
    session: Session,
    stitched: dict[str, Any],
    report: dict[str, Any],
    *,
    layout_mode: str = "preserve",
    target_standard: str = "auto",
) -> None:
    items = []
    for row in report["skipped"]:
        items.append({"kind": "device", "text": row.get("tag") or "", "sheet": row.get("sheet") or "", "reason": row.get("reason"), "confidence": 0})
    for row in report["unresolved_wires"]:
        # A parked bus or wire number keeps the confidence the model sent.
        # The distance, when measured, is how far the box sat from the conductor.
        item: dict[str, Any] = {
            "kind": "wire",
            "text": row.get("number") or "",
            "sheet": row.get("sheet") or "",
            "reason": row.get("reason"),
            "confidence": row.get("confidence") if row.get("confidence") is not None else 0,
        }
        if row.get("distance_mm") is not None:
            item["distance_mm"] = row["distance_mm"]
        if row.get("x") is not None and row.get("y") is not None:
            item["x"] = row["x"]
            item["y"] = row["y"]
        crop = str(row.get("crop") or "").strip()
        if crop:
            item["crop"] = crop
        items.append(item)
    for sheet in stitched.get("sheets") or []:
        for device in sheet.get("devices") or []:
            if float(device.get("confidence") or 1) < LOW_CONFIDENCE or device.get("library_guess"):
                items.append(
                    {
                        "kind": "device",
                        "text": device.get("tag") or "",
                        "sheet": sheet["id"],
                        "confidence": device.get("confidence") or 0,
                        "crop": device.get("crop") or "",
                        "reason": "low_confidence" if not device.get("library_guess") else "symbol_guess",
                    }
                )
        for item in sheet.get("unreadable") or []:
            reason = str(item.get("reason") or "")
            if str(item.get("field") or "") == "sheet_number":
                reason = "sheet_number"
            elif not reason:
                reason = "unreadable"
            row = {
                "kind": item.get("kind") or "text",
                "text": "",
                "sheet": sheet["id"],
                "reason": reason,
                "confidence": 0,
                "crop": item.get("crop") or "",
            }
            if item.get("detail"):
                row["detail"] = item["detail"]
            if item.get("field"):
                row["field"] = item["field"]
            items.append(row)
    xrefs = []
    for row in stitched.get("unknown_parts") or []:
        items.append(
            {
                "kind": "part",
                "text": row.get("catalog") or "",
                "tag": row.get("tag") or "",
                "sheet": "",
                "reason": "unknown",
                "confidence": row.get("confidence") or 0,
            }
        )
    for xref in stitched.get("unresolved_xrefs") or []:
        xrefs.append(
            {
                "text": xref.get("text") or xref.get("target") or "",
                "sheet": xref.get("sheet") or "",
                "target": xref.get("target") or "",
                "reason": xref.get("reason") or "unresolved",
            }
        )
    block = stitched.get("title_block") or {}
    numbers = {str(sheet.get("id") or ""): str(sheet.get("sheet_number") or "") for sheet in stitched.get("sheets") or []}
    sheet_needs_info = [
        str(sheet.get("id") or "") for sheet in stitched.get("sheets") or [] if sheet.get("sheet_number_needs_info")
    ]
    needs_info = any(str(row.get("reason") or "") == "needs_info" for row in report["skipped"]) or any(
        str(item.get("reason") or "") == "needs_info" for item in items
    )
    existing_pi = (session.project.properties or {}).get("print_import")
    from_to_sched = None
    if isinstance(existing_pi, dict) and isinstance(existing_pi.get("from_to_schedule"), dict):
        from_to_sched = existing_pi["from_to_schedule"]
    elif isinstance(stitched.get("from_to_schedule"), dict):
        from_to_sched = stitched["from_to_schedule"]
    else:
        try:
            from panelcad.importer.ladder_synth import extract_import_from_to

            from_to_sched = extract_import_from_to(stitched, catalog=session.catalog).to_dict()
        except Exception:  # noqa: BLE001
            from_to_sched = {}
    session.project.properties["print_import"] = {
        "items": items,
        "unresolved_xrefs": xrefs,
        "sheet_count": str(block.get("sheet_count") or ""),
        "sheet_numbers": numbers,
        "sheet_number_needs_info": sheet_needs_info,
        "not_for_build": "yes" if needs_info else "",
        "layout_mode": "ladder" if str(layout_mode).strip().lower() in {"ladder", "clean_ladder", "clean"} else "preserve",
        "target_standard": str(target_standard or "auto"),
        "from_to_schedule": from_to_sched or {},
    }


def _mst(devices: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if len(devices) < 2:
        return []
    remaining = set(range(1, len(devices)))
    tree = {0}
    edges: list[tuple[dict[str, Any], dict[str, Any]]] = []
    while remaining:
        best_i = 0
        best_j = next(iter(remaining))
        best_d = 1e18
        for i in tree:
            for j in remaining:
                dist = _dist(devices[i], devices[j])
                if dist < best_d:
                    best_d = dist
                    best_i, best_j = i, j
        edges.append((devices[best_i], devices[best_j]))
        tree.add(best_j)
        remaining.remove(best_j)
    return edges


def _dist(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x") or 0) - float(b.get("x") or 0)
    dy = float(a.get("y") or 0) - float(b.get("y") or 0)
    return dx * dx + dy * dy


def _seg_dist(px: float, py: float, a: dict[str, Any], b: dict[str, Any]) -> float:
    ax, ay = float(a.get("x") or 0), float(a.get("y") or 0)
    bx, by = float(b.get("x") or 0), float(b.get("y") or 0)
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length <= 1e-6:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
