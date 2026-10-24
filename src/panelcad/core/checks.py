"""Live design checks. One engine for the GUI, CLI, JSONL agent, and audit CSV.

``Session.validate()`` stays the structural error strings a script already
knows. This module adds severity, locations, suppressions, and the shop
checks (tags, dangling wires, schematic vs panel, ratings) on top of that
same walk. Do not invent a second validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from panelcad.core.circuit import is_coil_control_pin, is_motor_library
from panelcad.core.electrical import (
    LAYOUT_KINDS,
    SCHEMATIC_KINDS,
    _SKIP_CATEGORIES,
    _TERM_RE,
    audit_schematic_panel,
    is_coil_only_device,
    is_power_pole_device,
    symbol_role,
    wire_number_conflicts,
)
from panelcad.core.ladder import endpoint_is_rail, is_rail_ref, rail_names
from panelcad.core.layout import footprint_size, is_layout_sheet, rects_overlap
from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.library.catalog import Catalog
from panelcad.library.panel import effective_footprint, is_incabinet
from panelcad.library.ports import effective_ports
from panelcad.render.sheet import parse_pin_ref, pin_world

Severity = Literal["error", "warning", "info"]

SUPPRESSED_KEY = "suppressed_checks"
LIVE_DEBOUNCE_MS = 450

# Existing Session.validate() messages. New shop checks are not in this set so
# a clean drawing still returns [] from validate().
STRUCTURAL_CHECKS = frozenset(
    {
        "format",
        "duplicate_sheet",
        "duplicate_rail",
        "rail_length",
        "duplicate_tag",
        "unknown_library",
        "footprint_overlap",
        "wire_same_pin",
        "dangling_wire",
        "wire_number_conflict",
    }
)

CHECK_IDS: tuple[str, ...] = (
    "format",
    "duplicate_sheet",
    "duplicate_rail",
    "rail_length",
    "duplicate_tag",
    "duplicate_device_tag",
    "reference_target_missing",
    "unknown_library",
    "footprint_overlap",
    "wire_same_pin",
    "dangling_wire",
    "unconnected_device",
    "wire_number_conflict",
    "missing_panel",
    "missing_door",
    "missing_schematic",
    "terminal_missing",
    "terminal_not_on_strip",
    "missing_part_number",
    "unverified_part",
    "catalog_missing_dimensions",
    "catalog_poles",
    "catalog_current",
    "catalog_coil_voltage",
    "catalog_pilot_voltage",
    "catalog_secondary_voltage",
    "protection_over_downstream",
    "overload_outside_fla",
    "coil_voltage_mismatch",
    "outside_enclosure",
    "panel_needs_enclosure",
    "panel_enclosure_tight",
    "heat_rise_over_limit",
    "above_max_ambient",
    "nema4_filter_fan",
    "missing_footprint",
    "contact_without_coil",
    "unwired_coil",
    "contact_count_exceeded",
    "duplicate_contact_number",
    "broken_offpage",
    "offpage_missing_from",
    "control_power_margin",
    "control_power_inrush",
    "control_power_protection",
    "control_power_method",
    "sccr_below_available",
    "shield_ungrounded",
    "instrument_shield_both_ends",
    "wire_undersized",
    "protection_above_430_52",
    "overload_range_misses_fla",
    "overload_needs_nameplate",
    "breaker_frame_below_rating",
    "contactor_undersized",
    "duplicate_address",
    "placeholder_footprint",
    "layout_not_ready",
    "symbol_on_title_block",
    "soft_starter_overload",
    "cutout_unknown",
    "cutout_unresolved",
    "cutout_edge",
    "cutout_flange",
    "cutout_overlap",
    "cutout_clearance",
    "reconstruct_unconfirmed",
    "reconstruct_unscaled",
    "reconstruct_inferred_wire",
    "reconstruct_generic_layout",
    "reconstruct_print_mismatch",
    "reconstruct_print_unextracted",
    "reconstruct_print_missing_on_photos",
    "reconstruct_print_missing_on_print",
    "reconstruct_print_catalog_mismatch",
    "reconstruct_print_wire_missing",
    "reconstruct_unreadable",
    "reconstruct_needs_info",
    "reconstruct_scale_perspective",
    "nameplate_missing_field",
    "nameplate_sccr_below_available",
    "nameplate_fla_above_ocpd",
    "nameplate_flc_approximate",
    "nameplate_unconfirmed",
    "page_over_dense",
    "wire_end_off_pin",
    "label_covered_by_wire",
)

_AMP_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*A\b", re.I)
_NUM_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)")
_VOLT_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*V\s*(DC|AC)?", re.I)
_AWG_RE = re.compile(r"(?:#|AWG\s*)?(\d{1,2})\s*(?:AWG|GA|GAUGE)?\s*$", re.I)
_MM2_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*(?:MM2|MM²|MM\^2)\s*$", re.I)
_RANGE_RE = re.compile(
    r"([0-9]+(?:[.][0-9]+)?)\s*[-–]\s*([0-9]+(?:[.][0-9]+)?)\s*A?",
    re.I,
)

# Conservative chassis / control-panel ampacity. Used only when a gauge is set.
_AWG_AMPACITY = {18: 14.0, 16: 18.0, 14: 25.0, 12: 30.0, 10: 40.0, 8: 55.0, 6: 75.0, 4: 95.0}
_MM2_AMPACITY = {
    0.5: 6.0,
    0.75: 9.0,
    1.0: 12.0,
    1.5: 16.0,
    2.5: 24.0,
    4.0: 32.0,
    6.0: 41.0,
    10.0: 57.0,
    16.0: 76.0,
    25.0: 101.0,
}


@dataclass
class Finding:
    """One design problem. ``key()`` is stable so a shop can ignore it in YAML."""

    check: str
    severity: Severity
    message: str
    sheet: str = ""
    ref: str = ""
    target: str = ""
    x: float | None = None
    y: float | None = None
    suppressed: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.check}:{self.sheet}:{self.ref}:{self.target}"

    def to_row(self) -> dict[str, str]:
        return {
            "id": self.key(),
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "sheet": self.sheet,
            "ref": self.ref,
            "target": self.target,
            "x": "" if self.x is None else f"{self.x:g}",
            "y": "" if self.y is None else f"{self.y:g}",
            "suppressed": "yes" if self.suppressed else "",
            "library_id": self.extra.get("library_id", ""),
            "sheets": self.extra.get("sheets", ""),
            "photo_id": self.extra.get("photo_id", ""),
            "item_id": self.extra.get("item_id", ""),
            "action": self.extra.get("action", ""),
            "fix": self.extra.get("fix", ""),
            "crop": self.extra.get("crop", ""),
        }


def suppressed_ids(project: Project) -> list[str]:
    raw = project.properties.get(SUPPRESSED_KEY) or []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def set_suppressed_ids(project: Project, ids: Iterable[str]) -> None:
    project.properties[SUPPRESSED_KEY] = [str(item).strip() for item in ids if str(item).strip()]


def assert_pin_ref(project: Project, catalog: Catalog, sheet: Sheet, pin_ref: str) -> None:
    """Same rules as ``Session._assert_pin`` — rail name, pin, or comms port."""

    hot, ret = rail_names(sheet, project)
    # A bare rail word is a legal end. A device pin such as L1.X2 is not.
    if is_rail_ref(pin_ref, hot, ret):
        return
    comp_id, pin_id = parse_pin_ref(pin_ref)
    comp = sheet.component(comp_id)
    device = catalog.get(comp.library_id)
    try:
        device.pin(pin_id)
        return
    except KeyError:
        if any(p.id == pin_id for p in effective_ports(device, comp)):
            return
        # A print-import needs-info anchor keeps the pins the wire named.
        # A reconstruct trace can name a pin the symbol does not list. That is
        # still the existing device, not a new terminal.
        props = getattr(comp, "properties", None) or {}
        raw = str(props.get("print_import_pins") or "")
        named = str(props.get("reconstruct_named_pins") or "")
        allowed = {part.strip() for part in f"{raw},{named}".split(",") if part.strip()}
        if pin_id in allowed:
            return
    pins = ",".join(p.id for p in device.pins)
    raise KeyError(f"{device.library_id} has no pin or port '{pin_id}'. pins={pins}")


def structural_findings(project: Project, catalog: Catalog) -> list[Finding]:
    """The checks ``Session.validate()`` already ran — same messages, with locations."""

    findings: list[Finding] = []
    if project.format != "panelcad":
        findings.append(Finding("format", "error", "format must be panelcad"))
    seen_sheets: set[str] = set()
    for sheet in project.sheets:
        if sheet.id in seen_sheets:
            findings.append(
                Finding("duplicate_sheet", "error", f"duplicate sheet id {sheet.id}", sheet=sheet.id, ref=sheet.id)
            )
        seen_sheets.add(sheet.id)
        seen_refs: set[str] = set()
        seen_rails: set[str] = set()
        for rail in sheet.rails:
            if rail.id in seen_rails:
                findings.append(
                    Finding(
                        "duplicate_rail",
                        "error",
                        f"{sheet.id}: duplicate rail {rail.id}",
                        sheet=sheet.id,
                        ref=rail.id,
                        x=rail.x,
                        y=rail.y,
                    )
                )
            seen_rails.add(rail.id)
            if rail.length_mm <= 0:
                findings.append(
                    Finding(
                        "rail_length",
                        "error",
                        f"{sheet.id}:{rail.id} rail length must be > 0",
                        sheet=sheet.id,
                        ref=rail.id,
                        x=rail.x,
                        y=rail.y,
                    )
                )
        for comp in sheet.components:
            if comp.id in seen_refs:
                findings.append(
                    Finding(
                        "duplicate_tag",
                        "error",
                        f"{sheet.id}: duplicate ref {comp.id}",
                        sheet=sheet.id,
                        ref=comp.id,
                        x=comp.x,
                        y=comp.y,
                    )
                )
            seen_refs.add(comp.id)
            if comp.library_id not in catalog.devices:
                findings.append(
                    Finding(
                        "unknown_library",
                        "error",
                        f"{sheet.id}:{comp.id} unknown library_id {comp.library_id}",
                        sheet=sheet.id,
                        ref=comp.id,
                        x=comp.x,
                        y=comp.y,
                    )
                )
        if is_layout_sheet(sheet):
            boxes: list[tuple[str, float, float, float, float]] = []
            for comp in sheet.components:
                device = catalog.devices.get(comp.library_id)
                if device is None:
                    continue
                w, h = footprint_size(device, comp)
                for oid, ox, oy, ow, oh in boxes:
                    if rects_overlap(comp.x, comp.y, w, h, ox, oy, ow, oh):
                        findings.append(
                            Finding(
                                "footprint_overlap",
                                "error",
                                f"{sheet.id}: {comp.id} overlaps {oid}",
                                sheet=sheet.id,
                                ref=comp.id,
                                target=oid,
                                x=comp.x,
                                y=comp.y,
                            )
                        )
                boxes.append((comp.id, comp.x, comp.y, w, h))
            continue
        for wire in sheet.wires:
            if str(wire.frm).strip().upper() == str(wire.to).strip().upper():
                x, y = _wire_xy(sheet, catalog, wire)
                findings.append(
                    Finding(
                        "wire_same_pin",
                        "error",
                        f"{sheet.id}:{wire.id} from and to are the same pin ({wire.frm})",
                        sheet=sheet.id,
                        ref=wire.id,
                        target=str(wire.frm),
                        x=x,
                        y=y,
                    )
                )
            for end in (wire.frm, wire.to):
                try:
                    assert_pin_ref(project, catalog, sheet, end)
                except Exception as exc:  # noqa: BLE001 — collect, same as Session.validate
                    x, y = _end_xy(sheet, catalog, end) or _wire_xy(sheet, catalog, wire)
                    findings.append(
                        Finding(
                            "dangling_wire",
                            "error",
                            f"{sheet.id}:{wire.id} {exc}",
                            sheet=sheet.id,
                            ref=wire.id,
                            target=str(end),
                            x=x,
                            y=y,
                        )
                    )
    for message in wire_number_conflicts(project, catalog):
        findings.append(Finding("wire_number_conflict", "error", message))
    return findings


def structural_messages(project: Project, catalog: Catalog) -> list[str]:
    """Exact ``Session.validate()`` strings, in the same order."""

    return [item.message for item in structural_findings(project, catalog)]


def run_checks(
    project: Project,
    catalog: Catalog,
    *,
    include_suppressed: bool = False,
    parts: Any = None,
    project_path: Any = None,
    label_cover: bool = False,
) -> list[Finding]:
    """Every live check. Structural findings first, then shop design checks.

    *label_cover* runs the label placer for sheets not yet drawn. Off, the
    covered-label check reads only sheets whose labels are already placed, so
    a quick check on a large project stays quick.
    """

    findings = list(structural_findings(project, catalog))
    findings.extend(_duplicate_device_tags(project, catalog))
    findings.extend(_duplicate_io_addresses(project, catalog))
    findings.extend(_placeholder_footprints(project, catalog))
    findings.extend(_layout_not_ready(project, catalog))
    findings.extend(_unconnected_devices(project, catalog))
    findings.extend(_audit_findings(project, catalog))
    findings.extend(_terminal_findings(project, catalog))
    findings.extend(_missing_part_numbers(project, catalog))
    findings.extend(_unverified_parts(project, catalog))
    findings.extend(_catalog_missing_dimensions(project, catalog))
    findings.extend(_catalog_match_findings(project, catalog, parts))
    findings.extend(_protection_over_downstream(project, catalog))
    findings.extend(_overload_outside_fla(project, catalog))
    findings.extend(_coil_voltage_mismatch(project, catalog))
    findings.extend(_unwired_coils(project, catalog))
    findings.extend(_outside_enclosure(project, catalog))
    findings.extend(_panel_enclosure_findings(project, catalog))
    findings.extend(_on_title_block(project, catalog))
    findings.extend(_heat_rise_over_limit(project, catalog))
    findings.extend(_thermal_limit_findings(project, catalog))
    findings.extend(_missing_footprint(project, catalog))
    findings.extend(_soft_starter_without_overload(project))
    findings.extend(_xref_findings(project, catalog))
    findings.extend(_control_power_findings(project, catalog, parts))
    findings.extend(_sccr_findings(project, catalog))
    findings.extend(_cable_shield_findings(project))
    findings.extend(_sizing_findings(project, catalog, parts))
    findings.extend(_cutout_findings(project, catalog))
    findings.extend(_reconstruct_findings(project, project_path=project_path))
    findings.extend(_layout_not_ready_findings(project, catalog))
    findings.extend(_nameplate_findings(project, catalog))
    findings.extend(_print_import_findings(project))
    findings.extend(_power_column_overflow(project))
    findings.extend(_page_density(project, catalog))
    findings.extend(_wire_end_off_pin(project, catalog))
    findings.extend(_label_covered(project, catalog, compute=label_cover))
    from panelcad.core.transformer_jumpers import jumper_findings

    findings.extend(jumper_findings(project, catalog))
    hidden = set(suppressed_ids(project))
    out: list[Finding] = []
    seen: set[str] = set()
    for item in findings:
        key = item.key()
        if key in seen:
            continue
        seen.add(key)
        item.suppressed = key in hidden
        if item.suppressed and not include_suppressed:
            continue
        out.append(item)
    out.sort(key=_finding_rank)
    return out


# A wire end further than this (mm, on the sheet) from any symbol ink is
# drawn off the device.
OFF_PIN_TOL = 0.75


def _drawn_from_primitives(device: Device, comp: Component) -> bool:
    """True for symbols the generic painter draws from library primitives."""

    from panelcad.render.painter import is_control_coil

    props = comp.properties or {}
    if props.get("ladder_card") or props.get("ladder_coil"):
        return False
    cat = (device.category or "").lower()
    if cat.startswith("connection.notes") or not device.primitives:
        return False
    if is_control_coil(device, comp.library_id):
        return False
    try:
        from panelcad.render.io_ladder import is_io_module

        if is_io_module(device):
            return False
    except Exception:  # noqa: BLE001 - an odd device is not this check's problem
        return False
    return True


def _wire_end_off_pin(project: Project, catalog: Catalog) -> list[Finding]:
    """A wire whose end lands in white space beside the symbol it names."""

    from panelcad.core.pin_ink import pin_gap
    from panelcad.render.sheet import parse_pin_ref

    out: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind not in ("schematic", "io"):
            continue
        for wire in sheet.wires:
            for end in (wire.frm, wire.to):
                if "." not in str(end):
                    continue
                try:
                    cid, pid = parse_pin_ref(end)
                    comp = sheet.component(cid)
                except (KeyError, ValueError):
                    continue
                device = _device(catalog, comp)
                if device is None or not _drawn_from_primitives(device, comp):
                    continue
                pin = next((p for p in device.pins if p.id == pid), None)
                if pin is None:
                    continue
                gap = pin_gap(device, comp, pin) * comp.visual_scale()
                if gap <= OFF_PIN_TOL:
                    continue
                x, y = _end_xy(sheet, catalog, end) or (None, None)
                out.append(
                    Finding(
                        "wire_end_off_pin",
                        "warning",
                        f"{sheet.id}:{wire.id} ends {gap:.1f} mm off the {comp.id} symbol at pin {pid}. "
                        f"Library {comp.library_id} has no graphics at that pin; move the pin onto the body "
                        "or add a lead to the symbol.",
                        sheet=sheet.id,
                        ref=wire.id,
                        target=str(end),
                        x=x,
                        y=y,
                        extra={"library_id": comp.library_id},
                    )
                )
    return out


def _label_covered(project: Project, catalog: Catalog, *, compute: bool = True) -> list[Finding]:
    """Tags and wire numbers still crossed by a wire or another symbol after placement."""

    from panelcad.core.layout import is_generated_io_sheet, is_io_sheet
    from panelcad.render.label_avoid import covered_labels

    from panelcad.core.ladder import sheet_drawing_style

    out: list[Finding] = []
    for sheet_no, sheet in enumerate(project.sheets, start=1):
        if sheet.kind not in ("schematic", "io"):
            continue
        # Same choice draw_sheet_body makes, so the seats checked are the seats drawn.
        style = sheet_drawing_style(sheet, project)
        generated = is_generated_io_sheet(sheet)
        hand_io = is_io_sheet(sheet) and not generated
        ladder = (style == "ladder" and (sheet.kind == "schematic" or generated)) or (
            style == "iec" and sheet.kind == "schematic"
        )
        try:
            items = covered_labels(
                project, catalog, sheet, ladder=ladder, sheet_no=sheet_no, captions=not hand_io, peek=not compute
            )
        except (KeyError, ValueError, IndexError):
            continue
        for item in items or ():
            what = "tag" if item.kind == "tag" else "wire number"
            out.append(
                Finding(
                    "label_covered_by_wire",
                    "warning",
                    f"{sheet.id}: {what} '{item.text.splitlines()[0]}' is covered by {', '.join(item.by[:3])}. "
                    "No clear seat was found; move the device or open a gap.",
                    sheet=sheet.id,
                    ref=item.owner,
                    target=item.kind,
                    x=item.x,
                    y=item.y,
                )
            )
    return out


def _page_density(project: Project, catalog: Catalog) -> list[Finding]:
    """Warn when a schematic or I/O page holds more than its usable area allows."""

    from panelcad.core.spacing import page_fullness

    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.closed:
            continue
        row = page_fullness(sheet, catalog)
        if not row.get("over_dense"):
            continue
        findings.append(
            Finding(
                "page_over_dense",
                "warning",
                str(row.get("message") or f"Sheet {sheet.id} is over the page limit"),
                sheet=sheet.id,
                ref=sheet.id,
            )
        )
    return findings


def _power_column_overflow(project: Project) -> list[Finding]:
    """Cleanup records this when the columns still do not fit at the minimum scale."""

    raw = project.properties.get("power_column_overflow") or {}
    if not isinstance(raw, dict):
        return []
    findings: list[Finding] = []
    for sheet_id, message in raw.items():
        text = str(message or "").strip()
        if not text:
            continue
        findings.append(
            Finding(
                "power_columns_overflow",
                "warning",
                text,
                sheet=str(sheet_id),
                ref=str(sheet_id),
            )
        )
    return findings


# Import gaps stay above the per-device rows that a print import adds by the hundred.
_IMPORT_GAP_CHECKS = (
    "print_wire",
    "print_unplaced",
    "print_unreadable",
    "print_unknown_part",
    "print_unresolved_xref",
    "print_sheet_number",
)
_DEVICE_NOISE_CHECKS = (
    "unconnected_device",
    "missing_part_number",
    "missing_footprint",
    "print_low_confidence",
)


def _finding_rank(item: Finding) -> tuple:
    """Import gaps first, then other checks, then the repeated per-device rows."""

    if item.check in _IMPORT_GAP_CHECKS:
        band = 0
        group = _IMPORT_GAP_CHECKS.index(item.check)
    elif item.check in _DEVICE_NOISE_CHECKS:
        band = 2
        group = _DEVICE_NOISE_CHECKS.index(item.check)
    else:
        band = 1
        group = 0
    severity = {"error": 0, "warning": 1, "info": 2}.get(item.severity, 1)
    return (band, group, severity, item.check, item.sheet, item.ref, item.message)


def _reconstruct_findings(project: Project, project_path: Any = None) -> list[Finding]:
    from panelcad.reconstruct.checks import reconstruct_findings

    return reconstruct_findings(project, project_path=project_path)


def _has_layout_sheet(project: Project) -> bool:
    return any(sheet.kind in LAYOUT_KINDS for sheet in project.sheets)


def _layout_not_ready_findings(project: Project, catalog: Catalog) -> list[Finding]:
    """Error when a panel/door occupant has neither catalog dims nor a confirmed envelope.

    Schematic-only projects stay quiet. Once a layout sheet exists, generic
    table sizes are a build error for schematic occupants, I/O modules, and
    anything already on a layout. I/O ladder field devices stay on the I/O
    sheet (PR #40 ``bc4e3ca``).
    """

    from panelcad.library.panel import (
        LAYOUT_READY_FIX,
        is_info_needed_placeholder,
        is_layout_ready,
        layout_ready_gap,
        occupies_panel_layout,
    )

    if not _has_layout_sheet(project):
        return []
    findings: list[Finding] = []
    seen: set[str] = set()
    loc: dict[str, tuple[str, float, float]] = {}
    on_layout: set[str] = set()
    kinds: dict[str, set[str]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            loc.setdefault(comp.id, (sheet.id, comp.x, comp.y))
            kinds.setdefault(comp.id, set()).add(sheet.kind)
            if sheet.kind in LAYOUT_KINDS:
                on_layout.add(comp.id)
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            if is_info_needed_placeholder(comp):
                continue
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            if not occupies_panel_layout(device, comp):
                continue
            if is_layout_ready(device, comp):
                continue
            homes = kinds.get(comp.id, set())
            io_module = bool(
                (comp.properties or {}).get("io_module")
                or (comp.properties or {}).get("io_module_ref")
            )
            if not (comp.id in on_layout or "schematic" in homes or io_module):
                continue
            seen.add(comp.id)
            sheet_id, x, y = loc.get(comp.id, (sheet.id, comp.x, comp.y))
            gap = layout_ready_gap(device, comp)
            where = "on the layout" if comp.id in on_layout else "needed for the layout"
            findings.append(
                Finding(
                    "layout_not_ready",
                    "error",
                    f"{comp.id} {where}: {gap}",
                    sheet=sheet_id,
                    ref=comp.id,
                    x=x,
                    y=y,
                    extra={"library_id": comp.library_id, "fix": LAYOUT_READY_FIX, "action": "pick_catalog"},
                )
            )
    return findings


def _cutout_findings(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.core.cutout import cutout_check_rows

    findings: list[Finding] = []
    for row in cutout_check_rows(project, catalog):
        findings.append(
            Finding(
                str(row["check"]),
                "warning",
                str(row["message"]),
                sheet=str(row.get("sheet") or ""),
                ref=str(row.get("ref") or ""),
                target=str(row.get("target") or ""),
                x=row.get("x"),
                y=row.get("y"),
            )
        )
    return findings


def _control_power_findings(project: Project, catalog: Catalog, parts: Any) -> list[Finding]:
    from panelcad.core.power_budget import control_power_findings
    from panelcad.library.parts import default_parts_catalog

    catalog_parts = parts if parts is not None else default_parts_catalog()
    found: list[Finding] = []
    for item in control_power_findings(project, catalog, catalog_parts):
        found.append(
            Finding(
                item.check,
                item.severity,  # type: ignore[arg-type]
                item.message,
                sheet=item.sheet,
                ref=item.ref,
                target=item.target,
                x=item.x,
                y=item.y,
            )
        )
    return found


def _sccr_findings(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.electrical.sccr import sccr_findings

    return sccr_findings(project, catalog)


def _print_import_findings(project: Project) -> list[Finding]:
    payload = project.properties.get("print_import")
    if not isinstance(payload, dict) or not payload:
        return []
    from panelcad.importer.checks import findings_from_payload

    return findings_from_payload(payload)


def _sizing_findings(project: Project, catalog: Catalog, parts: Any = None) -> list[Finding]:
    from panelcad.core.sizing_checks import collect_sizing_issues

    findings: list[Finding] = []
    for row in collect_sizing_issues(project, catalog, parts):
        severity: Severity = "warning"
        raw = str(row.get("severity") or "warning")
        if raw in {"error", "warning", "info"}:
            severity = raw  # type: ignore[assignment]
        findings.append(
            Finding(
                str(row.get("check") or ""),
                severity,
                str(row.get("message") or ""),
                sheet=str(row.get("sheet") or ""),
                ref=str(row.get("ref") or ""),
                target=str(row.get("target") or ""),
                x=row.get("x"),
                y=row.get("y"),
            )
        )
    return findings


def _nameplate_findings(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.electrical.nameplate import nameplate_findings

    return nameplate_findings(project, catalog)


def _device(catalog: Catalog, comp: Component) -> Device | None:
    return catalog.devices.get(comp.library_id)


def _skip_device(device: Device | None) -> bool:
    if device is None:
        return True
    cat = device.category or ""
    return cat.startswith(_SKIP_CATEGORIES) or "offpage" in device.library_id or cat == "connection.notes"


def _merged_props(device: Device | None, comp: Component) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if device is not None:
        props.update(device.properties)
    props.update(comp.properties)
    return props


def _end_xy(sheet: Sheet, catalog: Catalog, pin_ref: str) -> tuple[float, float] | None:
    try:
        cid, pid = parse_pin_ref(pin_ref)
        comp = sheet.component(cid)
        device = catalog.devices.get(comp.library_id)
        if device is None:
            return (comp.x, comp.y)
        from panelcad.render.sheet import resolve_pin

        return pin_world(comp, resolve_pin(device, comp, pid))
    except Exception:  # noqa: BLE001 — location is best-effort
        try:
            cid, _pid = parse_pin_ref(pin_ref)
            comp = sheet.component(cid)
            return (comp.x, comp.y)
        except Exception:
            return None


def _wire_xy(sheet: Sheet, catalog: Catalog, wire: Wire) -> tuple[float | None, float | None]:
    a = _end_xy(sheet, catalog, wire.frm)
    b = _end_xy(sheet, catalog, wire.to)
    if a and b:
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    if a:
        return a
    if b:
        return b
    if wire.points:
        mid = wire.points[len(wire.points) // 2]
        return (mid.x, mid.y)
    return (None, None)


def _lands_on(sheet: Sheet, ref: str) -> list[Wire]:
    token = str(ref).upper()
    prefix = token + "."
    found: list[Wire] = []
    for wire in sheet.wires:
        for end in (wire.frm, wire.to):
            text = str(end).upper()
            if text == token or text.startswith(prefix):
                found.append(wire)
                break
    return found


def _duplicate_device_tags(project: Project, catalog: Catalog) -> list[Finding]:
    """Two independent schematic devices that reused a tag. Coil + contact is fine.

    A copy on a network or overview sheet, a rack view of an I/O module, or a
    copy marked with ``mark_reference`` points at the primary device and is
    not a second device (see ``core.references``).
    """

    from panelcad.core.references import reference_map, reference_target

    refs = reference_map(project, catalog)
    findings: list[Finding] = []
    groups: dict[str, list[tuple[Sheet, Component, Device | None]]] = {}
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS and sheet.kind != "network":
            continue
        for comp in sheet.components:
            link = refs.get(id(comp))
            target = reference_target(comp)
            if link is not None and (not target or link.primary_sheet.id == target):
                continue
            if target:
                findings.append(
                    Finding(
                        "reference_target_missing",
                        "warning",
                        f"{comp.id} on {sheet.id} is marked as a reference of sheet {target}, "
                        f"but no primary {comp.id} was found there",
                        sheet=sheet.id,
                        ref=comp.id,
                        target=target,
                        x=comp.x,
                        y=comp.y,
                    )
                )
            if link is not None:
                continue
            device = _device(catalog, comp)
            if _skip_device(device):
                continue
            groups.setdefault(comp.id, []).append((sheet, comp, device))
    for ref, items in groups.items():
        if len(items) < 2:
            continue
        roles = [symbol_role(device) for _s, _c, device in items]
        if all(role == "contact" for role in roles):
            continue
        # Power poles on one sheet and the coil on another are one contactor.
        if _split_coil_and_poles(items):
            continue
        # One coil/parent plus its contacts is a family, not a clash.
        if all(role in {"coil", "contact", "parent"} for role in roles) and sum(
            1 for role in roles if role in {"coil", "parent"}
        ) <= 1:
            continue
        first_sheet, first, _d = items[0]
        others = ",".join(f"{sheet.id}:{comp.id}" for sheet, comp, _dev in items[1:])
        findings.append(
            Finding(
                "duplicate_device_tag",
                "error",
                f"{ref} is used as a device tag on more than one schematic ({first_sheet.id} and {others})",
                sheet=first_sheet.id,
                ref=ref,
                target=others,
                x=first.x,
                y=first.y,
            )
        )
    return findings


def _split_coil_and_poles(items: list[tuple[Sheet, Component, Device | None]]) -> bool:
    """One power-pole symbol plus at most one coil symbol, and their contacts."""

    poles = 0
    coils = 0
    for _sheet, _comp, device in items:
        if symbol_role(device) == "contact":
            continue
        if is_power_pole_device(device):
            poles += 1
        elif is_coil_only_device(device):
            coils += 1
        else:
            return False
    return poles == 1 and coils <= 1


def _duplicate_io_addresses(project: Project, catalog: Catalog) -> list[Finding]:
    """Two I/O points that print the same platform address."""

    from panelcad.core.electrical import _module_sheet_address

    seen: dict[str, tuple[str, str]] = {}
    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            if not comp.properties.get("io_module"):
                continue
            device = _device(catalog, comp)
            if device is None:
                continue
            for pin in device.pins:
                text, _style = _module_sheet_address(catalog, comp, pin.id)
                if not text:
                    continue
                prior = seen.get(text)
                if prior is None:
                    seen[text] = (comp.id, pin.id)
                    continue
                findings.append(
                    Finding(
                        "duplicate_address",
                        "warning",
                        f"{comp.id}.{pin.id} repeats address {text} already used by {prior[0]}.{prior[1]}",
                        sheet=sheet.id,
                        ref=comp.id,
                        target=text,
                        x=comp.x,
                        y=comp.y,
                    )
                )
    return findings


def _has_layout_sheet(project: Project) -> bool:
    return any(sheet.kind in LAYOUT_KINDS for sheet in project.sheets)


def _layout_not_ready(project: Project, catalog: Catalog) -> list[Finding]:
    """Error when a panel/door occupant has neither catalog dims nor a confirmed envelope.

    Schematic-only projects stay quiet. Once a layout sheet exists (or a part
    already sits on one), generic table sizes are a build error.
    """

    from panelcad.library.panel import (
        LAYOUT_READY_FIX,
        is_layout_ready,
        layout_ready_gap,
        occupies_panel_layout,
    )

    if not _has_layout_sheet(project):
        return []
    findings: list[Finding] = []
    seen: set[str] = set()
    loc: dict[str, tuple[str, float, float]] = {}
    on_layout: set[str] = set()
    kinds: dict[str, set[str]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            loc.setdefault(comp.id, (sheet.id, comp.x, comp.y))
            kinds.setdefault(comp.id, set()).add(sheet.kind)
            if sheet.kind in LAYOUT_KINDS:
                on_layout.add(comp.id)
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            device = _device(catalog, comp)
            if device is None:
                continue
            if not occupies_panel_layout(device, comp):
                continue
            if is_layout_ready(device, comp):
                continue
            homes = kinds.get(comp.id, set())
            io_module = bool(
                (comp.properties or {}).get("io_module")
                or (comp.properties or {}).get("io_module_ref")
            )
            # I/O ladder field devices stay on the I/O sheet. Flag the module
            # (needed for the rack), schematic occupants, and anything already
            # sitting on a layout.
            if not (
                comp.id in on_layout
                or "schematic" in homes
                or io_module
            ):
                continue
            seen.add(comp.id)
            sheet_id, x, y = loc.get(comp.id, (sheet.id, comp.x, comp.y))
            gap = layout_ready_gap(device, comp)
            where = "on the layout" if comp.id in on_layout else "needed for the layout"
            findings.append(
                Finding(
                    "layout_not_ready",
                    "error",
                    f"{comp.id} {where}: {gap}",
                    sheet=sheet_id,
                    ref=comp.id,
                    x=x,
                    y=y,
                    extra={"library_id": comp.library_id, "fix": LAYOUT_READY_FIX},
                )
            )
    return findings


def layout_build_blockers(project: Project, catalog: Catalog) -> list[Finding]:
    """Unready parts that already sit on a backpanel or door.

    Schematic occupants still show as Problems (needed for the layout) so they
    can be fixed before packing. They do not hold a from-to or revision print.
    """

    return [
        item
        for item in _layout_not_ready(project, catalog)
        if item.check == "layout_not_ready" and "on the layout" in item.message
    ]


def _blocker_is_needs_info_placeholder(project: Project, ref: str) -> bool:
    """True when every layout copy of ``ref`` is a reconstruct.needs_info marker."""

    from panelcad.reconstruct.placeholder import is_needs_info_component

    found = False
    for sheet in project.sheets:
        if sheet.kind not in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            if comp.id != ref:
                continue
            found = True
            if not is_needs_info_component(comp):
                return False
    return found


def require_layout_ready_for_build(
    project: Project,
    catalog: Catalog,
    *,
    allow_needs_info_placeholders: bool = False,
) -> None:
    """Block print-package and quote while a laid-out part is still generic.

    ``allow_needs_info_placeholders`` lets a print package through when the
    only laid-out parts that are not ready are reconstruct INFO NEEDED
    markers. Those pages are stamped NOT FOR BUILD. A real generic or any
    other unconfirmed part still raises. Quote and build pricing do not use
    this allowance.
    """

    from panelcad.library.panel import LAYOUT_READY_FIX

    rows = layout_build_blockers(project, catalog)
    if allow_needs_info_placeholders:
        rows = [
            item
            for item in rows
            if not _blocker_is_needs_info_placeholder(project, item.ref)
        ]
    if not rows:
        return
    refs = ", ".join(item.ref for item in rows[:16])
    extra = f", … and {len(rows) - 16} more" if len(rows) > 16 else ""
    raise ValueError(
        f"NOT FOR BUILD: {len(rows)} part(s) need a catalog part or a confirmed "
        f"envelope before a layout print package or quote can be exported: "
        f"{refs}{extra}. {LAYOUT_READY_FIX}"
    )


def _placeholder_footprints(project: Project, catalog: Catalog) -> list[Finding]:
    """Generic module outlines are not maker dimensions."""

    from panelcad.library.panel import effective_footprint

    findings: list[Finding] = []
    seen: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            if not (comp.properties.get("io_module") or comp.properties.get("io_module_ref")):
                continue
            device = _device(catalog, comp)
            if device is None:
                continue
            fp = effective_footprint(device, comp)
            marked = str(comp.properties.get("placeholder_footprint") or "").lower() in {"1", "true", "yes"}
            if fp.dim_source == "catalog" and not marked:
                continue
            seen.add(comp.id)
            findings.append(
                Finding(
                    "placeholder_footprint",
                    "warning",
                    f"{comp.id} footprint is a placeholder, not a maker dimension",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _unconnected_devices(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            role = symbol_role(device)
            if role == "terminal":
                continue
            if role == "contact":
                continue
            if not device.pins:
                continue
            if _lands_on(sheet, comp.id):
                continue
            findings.append(
                Finding(
                    "unconnected_device",
                    "warning",
                    f"{sheet.id}:{comp.id} has no wires",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _layout_fit_reason(project: Project, ref: str) -> str:
    """Why the last Build layout left ``ref`` off, if that enclosure is still the current one."""

    stored = project.properties.get("layout_unplaced") or {}
    if not isinstance(stored, dict):
        return ""
    if "reasons" in stored:
        reasons = stored.get("reasons") or {}
        reason = str(reasons.get(ref) or "") if isinstance(reasons, dict) else ""
        if not reason:
            return ""
        sheet = next((item for item in project.sheets if item.id == stored.get("sheet")), None)
        enc = sheet.enclosure if sheet is not None else None
        if enc is None:
            return "Build layout again for the new enclosure size."
        same = (
            abs(float(enc.width_mm) - float(stored.get("width_mm") or 0.0)) <= 0.5
            and abs(float(enc.height_mm) - float(stored.get("height_mm") or 0.0)) <= 0.5
            and abs(float(enc.depth_mm) - float(stored.get("depth_mm") or enc.depth_mm)) <= 0.5
        )
        if same:
            return reason
        return "Build layout again for the new enclosure size."
    return str(stored.get(ref) or "")


def _audit_findings(project: Project, catalog: Catalog) -> list[Finding]:
    loc: dict[str, tuple[str, float, float]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            loc.setdefault(comp.id, (sheet.id, comp.x, comp.y))
    findings: list[Finding] = []
    for row in audit_schematic_panel(project, catalog):
        issue = str(row.get("issue") or "")
        ref = str(row.get("ref") or "")
        sheets = str(row.get("sheets") or "")
        sheet_id = sheets.split(",", 1)[0] if sheets else loc.get(ref, ("", 0.0, 0.0))[0]
        xy = loc.get(ref)
        if issue == "missing_panel":
            message = f"{ref} is on the schematic but missing from the panel layout"
            reason = _layout_fit_reason(project, ref)
            if reason:
                message = f"{message}. {reason}"
        elif issue == "missing_door":
            message = f"{ref} is on the schematic but missing from the door layout"
        elif issue == "missing_schematic":
            message = f"{ref} is on the layout but missing from the schematic"
        else:
            message = f"{ref} {issue}"
        findings.append(
            Finding(
                issue or "audit",
                "warning",
                message,
                sheet=sheet_id,
                ref=ref,
                target=str(row.get("library_id") or ""),
                x=xy[1] if xy else None,
                y=xy[2] if xy else None,
                extra={"library_id": str(row.get("library_id") or ""), "sheets": sheets},
            )
        )
    return findings


def _terminal_assigned(comp: Component) -> bool:
    if str(comp.properties.get("strip") or "").strip():
        return True
    return bool(_TERM_RE.match(comp.id))


def _terminal_ref_prefixes(catalog: Catalog) -> set[str]:
    """Letter prefixes of real terminal-block symbols (X, GND), not device tags."""

    prefixes: set[str] = set()
    for device in catalog.devices.values():
        if symbol_role(device) != "terminal":
            continue
        prefix = str(device.ref_prefix or "").strip().upper()
        if prefix:
            prefixes.add(prefix)
    return prefixes or {"X"}


def _is_terminal_block_tag(cid: str, catalog: Catalog) -> bool:
    """True when ``cid`` is a strip terminal (X1-4), not a device contact (M1-2, CR1-13)."""

    match = _TERM_RE.match(cid)
    if not match:
        return False
    strip = match.group(1).upper()
    for prefix in _terminal_ref_prefixes(catalog):
        if strip == prefix:
            return True
        if strip.startswith(prefix) and strip[len(prefix) :].isdigit():
            return True
    return False


def _terminal_findings(project: Project, catalog: Catalog) -> list[Finding]:
    terminals: dict[str, tuple[Sheet, Component]] = {}
    devices: set[str] = set()
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if symbol_role(device) != "terminal":
                devices.add(comp.id)
                continue
            terminals[comp.id] = (sheet, comp)
    findings: list[Finding] = []
    for sheet, comp in terminals.values():
        if _terminal_assigned(comp):
            continue
        findings.append(
            Finding(
                "terminal_not_on_strip",
                "warning",
                f"{sheet.id}:{comp.id} is a terminal but is not on a strip",
                sheet=sheet.id,
                ref=comp.id,
                x=comp.x,
                y=comp.y,
            )
        )
    seen: set[str] = set()
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        for wire in sheet.wires:
            for end in (wire.frm, wire.to):
                try:
                    cid, _pid = parse_pin_ref(end)
                except ValueError:
                    continue
                if cid in terminals or cid in devices:
                    continue
                if not _is_terminal_block_tag(cid, catalog):
                    continue
                key = f"{sheet.id}:{wire.id}:{cid}"
                if key in seen:
                    continue
                seen.add(key)
                x, y = _end_xy(sheet, catalog, end) or _wire_xy(sheet, catalog, wire)
                findings.append(
                    Finding(
                        "terminal_missing",
                        "warning",
                        f"{sheet.id}:{wire.id} references terminal {cid} which is not on a strip",
                        sheet=sheet.id,
                        ref=wire.id,
                        target=cid,
                        x=x,
                        y=y,
                    )
                )
    return findings


def _missing_part_numbers(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            if device.category == "connection.accessory":
                continue
            if symbol_role(device) == "contact":
                continue
            props = _merged_props(device, comp)
            if props.get("addon") or props.get("layout_furniture"):
                continue
            if comp.properties.get("io_owner") and not comp.properties.get("io_module"):
                continue
            if not is_incabinet(device, comp) and sheet.kind in LAYOUT_KINDS:
                continue
            part = str(props.get("part_number") or props.get("catalog") or "").strip()
            if part:
                seen.add(comp.id)
                continue
            seen.add(comp.id)
            findings.append(
                Finding(
                    "missing_part_number",
                    "info",
                    f"{comp.id} has no part number",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _unverified_parts(project: Project, catalog: Catalog) -> list[Finding]:
    """Info when a placed device uses a partially verified catalog entry."""

    findings: list[Finding] = []
    seen: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            verified = str(comp.properties.get("catalog_verified") or "").strip().lower()
            if verified != "partial":
                continue
            seen.add(comp.id)
            mfr = str(comp.properties.get("manufacturer") or "").strip()
            sku = str(comp.properties.get("catalog") or comp.properties.get("part_number") or "").strip()
            label = " ".join(bit for bit in (mfr, sku) if bit) or "catalog part"
            findings.append(
                Finding(
                    "unverified_part",
                    "info",
                    f"{comp.id} uses partially verified {label}",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                    extra={"library_id": comp.library_id},
                )
            )
    return findings


def _catalog_missing_dimensions(project: Project, catalog: Catalog) -> list[Finding]:
    """Info when an assigned catalog SKU did not publish a width and height.

    Enclosure packing still uses the generic symbol size. This flag tells
    auto-sizing that those millimetres are not the maker's drawing.
    """

    findings: list[Finding] = []
    seen: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            sku = str(comp.properties.get("catalog") or comp.properties.get("part_number") or "").strip()
            if not sku:
                continue
            fp = effective_footprint(device, comp)
            if str(fp.dim_source or "") == "catalog":
                continue
            seen.add(comp.id)
            mfr = str(comp.properties.get("manufacturer") or "").strip()
            label = " ".join(bit for bit in (mfr, sku) if bit)
            findings.append(
                Finding(
                    "catalog_missing_dimensions",
                    "info",
                    f"{comp.id} catalog {label} has no published width/height; enclosure sizing is using the generic symbol",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                    extra={"library_id": comp.library_id, "catalog": sku},
                )
            )
    return findings


def parse_amps(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _AMP_RE.search(text)
    if match:
        return float(match.group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def parse_amp_range(value: Any) -> tuple[float, float] | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _RANGE_RE.search(text)
    if not match:
        return None
    low, high = float(match.group(1)), float(match.group(2))
    if high < low:
        low, high = high, low
    return (low, high)


def parse_volts(value: Any) -> tuple[float, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _VOLT_RE.search(text.replace(" ", ""))
    if not match:
        return None
    kind = (match.group(2) or "").lower()
    return (float(match.group(1)), kind)


def gauge_ampacity(gauge: str) -> float | None:
    text = str(gauge or "").strip()
    if not text:
        return None
    compact = text.replace("²", "2").replace("^2", "2")
    mm = _MM2_RE.match(compact.replace(" ", ""))
    if mm:
        size = float(mm.group(1))
        if size in _MM2_AMPACITY:
            return _MM2_AMPACITY[size]
        nearest = min(_MM2_AMPACITY, key=lambda item: abs(item - size))
        return _MM2_AMPACITY[nearest]
    awg = _AWG_RE.match(text.strip())
    if awg:
        size = int(awg.group(1))
        return _AWG_AMPACITY.get(size)
    mm_plain = _NUM_RE.fullmatch(text)
    if mm_plain:
        size = float(mm_plain.group(1))
        if size in _MM2_AMPACITY:
            return _MM2_AMPACITY[size]
    return None


def _device_amps(device: Device | None, comp: Component) -> float | None:
    props = _merged_props(device, comp)
    for key in ("In", "Ie", "Ith", "current", "Iout", "Ir", "FLA", "fla"):
        amps = parse_amps(props.get(key))
        if amps is not None:
            return amps
    return None


def _is_switched_path(device: Device | None) -> bool:
    """A contactor or relay is not the OCPD's downstream rating."""

    if device is None:
        return False
    role = symbol_role(device)
    if role in {"coil", "parent", "contact"}:
        return True
    cat = (device.category or "").lower()
    return cat.startswith("switching.")


def _is_protection(device: Device | None) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = device.library_id.lower()
    return (
        cat.startswith("protection.breaker")
        or cat.startswith("protection.fuse")
        or cat.startswith("protection.mpcb")
        or "fuse" in lid
        or "breaker" in lid
        or "mcb" in lid
        or "mccb" in lid
        or "mpcb" in lid
    )


def _load_pins(device: Device) -> set[str]:
    pins = list(device.pins)
    if not pins:
        return set()
    bottoms = {p.id for p in pins if p.side == "bottom"}
    if bottoms:
        return bottoms
    even = {p.id for p in pins if p.id.isdigit() and int(p.id) % 2 == 0}
    if even:
        return even
    if len(pins) == 2:
        return {pins[-1].id}
    return {p.id for p in pins}


def _adjacency(sheet: Sheet) -> dict[str, list[tuple[str, Wire]]]:
    graph: dict[str, list[tuple[str, Wire]]] = {}
    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        graph.setdefault(f"{a_ref}.{a_pin}", []).append((f"{b_ref}.{b_pin}", wire))
        graph.setdefault(f"{b_ref}.{b_pin}", []).append((f"{a_ref}.{a_pin}", wire))
    return graph


def _protection_over_downstream(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        graph = _adjacency(sheet)
        for comp in sheet.components:
            device = _device(catalog, comp)
            if not _is_protection(device) or device is None:
                continue
            rating = _device_amps(device, comp)
            if rating is None:
                continue
            sized = str(comp.properties.get("sized_by") or "") == "nec-430"
            start = [f"{comp.id}.{pid}" for pid in _load_pins(device)]
            seen: set[str] = {f"{comp.id}.{p.id}" for p in device.pins}
            queue = list(start)
            wires_seen: set[str] = set()
            while queue:
                token = queue.pop()
                for nxt, wire in graph.get(token, []):
                    if wire.id not in wires_seen:
                        wires_seen.add(wire.id)
                        cap = gauge_ampacity(wire.gauge)
                        if cap is not None and rating > cap + 0.05 and not sized:
                            x, y = _wire_xy(sheet, catalog, wire)
                            findings.append(
                                Finding(
                                    "protection_over_downstream",
                                    "warning",
                                    f"{comp.id} is rated {rating:g}A above {wire.id} ({wire.gauge or 'wire'} {cap:g}A)",
                                    sheet=sheet.id,
                                    ref=comp.id,
                                    target=wire.id,
                                    x=x,
                                    y=y,
                                )
                            )
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    try:
                        nref, _npin = parse_pin_ref(nxt)
                    except ValueError:
                        continue
                    if nref == comp.id:
                        continue
                    other = None
                    try:
                        other_comp = sheet.component(nref)
                        other = _device(catalog, other_comp)
                    except KeyError:
                        other_comp = None
                    other_amps = _device_amps(other, other_comp) if other_comp is not None else None
                    if (
                        other_amps is not None
                        and rating > other_amps + 0.05
                        and not _is_switched_path(other)
                        and not sized
                    ):
                        findings.append(
                            Finding(
                                "protection_over_downstream",
                                "warning",
                                f"{comp.id} is rated {rating:g}A above downstream {nref} ({other_amps:g}A)",
                                sheet=sheet.id,
                                ref=comp.id,
                                target=nref,
                                x=comp.x,
                                y=comp.y,
                            )
                        )
                    if other is None or _is_switched_path(other) or _is_overload(other):
                        queue.append(nxt)
                    elif not _is_protection(other):
                        queue.append(nxt)
    return findings


def _is_overload(device: Device | None) -> bool:
    if device is None:
        return False
    if symbol_role(device) == "contact":
        return False
    cat = (device.category or "").lower()
    lid = device.library_id.lower()
    return cat.startswith("protection.overload") or "overload" in lid


def _is_mpcb(device: Device | None) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = device.library_id.lower()
    return cat.startswith("protection.mpcb") or "mpcb" in lid


def _is_adjustable_protection(device: Device | None) -> bool:
    """Thermal-adjustable devices: overload relays and motor protectors."""

    return _is_overload(device) or _is_mpcb(device)


def _amp_range_from_props(props: dict[str, Any]) -> tuple[float, float] | None:
    rng = parse_amp_range(props.get("range"))
    if rng:
        return rng
    low, high = props.get("amps_min"), props.get("amps_max")
    if low in (None, "") or high in (None, ""):
        return None
    try:
        amin, amax = float(low), float(high)
    except (TypeError, ValueError):
        return None
    if amax < amin:
        amin, amax = amax, amin
    return (amin, amax)


def _overload_setting(device: Device | None, comp: Component) -> float | None:
    props = _merged_props(device, comp)
    for key in ("setting", "Ir", "FLA", "fla", "In"):
        amps = parse_amps(props.get(key))
        if amps is not None:
            return amps
    return None


def _motor_fla(device: Device | None, comp: Component) -> float | None:
    props = _merged_props(device, comp)
    for key in ("FLA", "fla", "In", "Ie", "Iout", "current"):
        amps = parse_amps(props.get(key))
        if amps is not None:
            return amps
    return None


def _connected_refs(sheet: Sheet, start: str) -> set[str]:
    graph = _adjacency(sheet)
    seen: set[str] = set()
    refs: set[str] = {start}
    queue = [key for key in graph if key.startswith(start + ".")]
    while queue:
        token = queue.pop()
        if token in seen:
            continue
        seen.add(token)
        try:
            refs.add(parse_pin_ref(token)[0])
        except ValueError:
            pass
        for nxt, _wire in graph.get(token, []):
            if nxt not in seen:
                queue.append(nxt)
    return refs


def _overload_outside_fla(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        overloads: list[tuple[Component, Device]] = []
        motors: list[tuple[Component, Device]] = []
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            if _is_adjustable_protection(device):
                overloads.append((comp, device))
            elif is_motor_library(comp.library_id) or (device.category or "").startswith("load.motor"):
                motors.append((comp, device))
        for ol, ol_dev in overloads:
            setting = _overload_setting(ol_dev, ol)
            props = _merged_props(ol_dev, ol)
            rng = _amp_range_from_props(props)
            named = str(ol.properties.get("motor") or "").strip()
            partners: list[tuple[Component, Device]] = []
            if named:
                try:
                    motor = sheet.component(named)
                    mdev = _device(catalog, motor)
                    if mdev is not None:
                        partners.append((motor, mdev))
                except KeyError:
                    partners = []
            if not partners:
                connected = _connected_refs(sheet, ol.id)
                partners = [(m, d) for m, d in motors if m.id in connected]
            if not partners and len(motors) == 1:
                partners = list(motors)
            for motor, mdev in partners:
                ol_current = parse_amps(ol.properties.get("ol_fla"))
                fla = ol_current if ol_current is not None else _motor_fla(mdev, motor)
                if fla is None or setting is None:
                    continue
                low, high = fla * 0.9, fla * 1.25
                if rng:
                    low = max(low, rng[0])
                    high = min(high, rng[1] * 1.01)
                if setting + 1e-6 < low or setting - 1e-6 > high:
                    findings.append(
                        Finding(
                            "overload_outside_fla",
                            "warning",
                            f"{ol.id} setting {setting:g}A is outside motor {motor.id} FLA {fla:g}A",
                            sheet=sheet.id,
                            ref=ol.id,
                            target=motor.id,
                            x=ol.x,
                            y=ol.y,
                        )
                    )
            if setting is not None and rng and not partners:
                if setting + 1e-6 < rng[0] or setting - 1e-6 > rng[1]:
                    findings.append(
                        Finding(
                            "overload_outside_fla",
                            "warning",
                            f"{ol.id} setting {setting:g}A is outside overload range {rng[0]:g}–{rng[1]:g}A",
                            sheet=sheet.id,
                            ref=ol.id,
                            x=ol.x,
                            y=ol.y,
                        )
                    )
    return findings


def _hint_voltage(text: str) -> tuple[float, str] | None:
    parsed = parse_volts(text)
    if parsed:
        return parsed
    token = "".join(ch for ch in str(text).upper() if ch.isalnum())
    table = {
        "24VDC": (24.0, "dc"),
        "24VAC": (24.0, "ac"),
        "24V": (24.0, ""),
        "0VDC": (0.0, "dc"),
        "120VAC": (120.0, "ac"),
        "120V": (120.0, "ac"),
        "230VAC": (230.0, "ac"),
        "240VAC": (240.0, "ac"),
    }
    return table.get(token)


def _rail_voltage(sheet: Sheet, project: Project) -> tuple[float, str] | None:
    hot, ret = rail_names(sheet, project)
    for name in (hot, ret, f"{hot}/{ret}"):
        hint = _hint_voltage(name)
        if hint and hint[0] > 0:
            return hint
    return None


def _end_voltage(sheet: Sheet, project: Project, pin_ref: str, wire: Wire) -> tuple[float, str] | None:
    for raw in (wire.net, wire.label, wire.number):
        hint = _hint_voltage(str(raw or ""))
        if hint and hint[0] > 0:
            return hint
    if endpoint_is_rail(sheet, pin_ref, project):
        return _rail_voltage(sheet, project)
    return None


def _voltages_conflict(coil: tuple[float, str], circuit: tuple[float, str]) -> bool:
    if circuit[0] <= 0:
        return False
    if abs(coil[0] - circuit[0]) > 0.6:
        return True
    if coil[1] and circuit[1] and coil[1] != circuit[1]:
        return True
    return False


def _coil_voltage_mismatch(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            role = symbol_role(device)
            if role not in {"coil", "parent"} and not is_coil_control_pin(comp.library_id, "A1"):
                continue
            props = _merged_props(device, comp)
            coil = parse_volts(props.get("coil") or props.get("voltage") or props.get("Un"))
            if coil is None:
                continue
            landed = _lands_on(sheet, comp.id)
            if not landed:
                continue
            circuit: tuple[float, str] | None = None
            for wire in landed:
                for end in (wire.frm, wire.to):
                    try:
                        cid, _pid = parse_pin_ref(end)
                    except ValueError:
                        cid = ""
                    if cid == comp.id:
                        other = wire.to if str(wire.frm).upper().startswith(comp.id.upper() + ".") else wire.frm
                        circuit = _end_voltage(sheet, project, other, wire)
                    else:
                        circuit = _end_voltage(sheet, project, end, wire)
                    if circuit:
                        break
                if circuit:
                    break
            if circuit is None:
                continue
            if _voltages_conflict(coil, circuit):
                kind = (coil[1] or "").upper()
                findings.append(
                    Finding(
                        "coil_voltage_mismatch",
                        "warning",
                        f"{comp.id} coil {coil[0]:g}V{kind} does not match its control circuit "
                        f"({circuit[0]:g}V{(circuit[1] or '').upper()})",
                        sheet=sheet.id,
                        ref=comp.id,
                        x=comp.x,
                        y=comp.y,
                    )
                )
    return findings


def _unwired_coils(project: Project, catalog: Catalog) -> list[Finding]:
    """A contactor coil with no wire on A1 or A2 is not a control circuit."""

    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        landed: set[str] = set()
        for wire in sheet.wires:
            landed.add(wire.frm)
            landed.add(wire.to)
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            pins = {pin.id for pin in device.pins}
            if "A1" not in pins or "A2" not in pins:
                continue
            if not is_coil_control_pin(comp.library_id, "A1"):
                continue
            # The power-pole symbol still has A1/A2. The coil is the one on the ladder.
            if is_power_pole_device(device) and _coil_drawn_separately(project, catalog, comp):
                continue
            open_pins = [pin for pin in ("A1", "A2") if f"{comp.id}.{pin}" not in landed]
            if not open_pins:
                continue
            findings.append(
                Finding(
                    "unwired_coil",
                    "warning",
                    f"{comp.id} contactor coil has no connection on {', '.join(open_pins)}",
                    sheet=sheet.id,
                    ref=comp.id,
                    target=",".join(open_pins),
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _coil_drawn_separately(project: Project, catalog: Catalog, comp: Component) -> bool:
    """True when this tag also has a coil-only symbol on a schematic sheet."""

    for sheet in project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        for other in sheet.components:
            if other.id != comp.id or other is comp:
                continue
            if is_coil_only_device(_device(catalog, other)):
                return True
    return False


def _catalog_match_findings(project: Project, catalog: Catalog, parts: Any = None) -> list[Finding]:
    from panelcad.core.catalog_match import collect_catalog_mismatches

    findings: list[Finding] = []
    for item in collect_catalog_mismatches(project, catalog, parts):
        findings.append(
            Finding(
                str(item["check"]),
                item["severity"],  # type: ignore[arg-type]
                str(item["message"]),
                sheet=str(item.get("sheet") or ""),
                ref=str(item.get("ref") or ""),
                target=str(item.get("target") or ""),
                x=item.get("x"),
                y=item.get("y"),
            )
        )
    return findings


def _on_title_block(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.core.ink import schematic_ink_rect
    from panelcad.core.layout import symbol_body_size
    from panelcad.render.titleblock import body_hits_title_block, title_block_rect

    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        tx, ty, tw, _th = title_block_rect(sheet)
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            width, height = symbol_body_size(sheet, device, comp)
            hit = body_hits_title_block(sheet, comp.x, comp.y, width, height)
            if not hit and str(comp.properties.get("role") or "") not in {"to", "from"}:
                ink = schematic_ink_rect(device, comp.x, comp.y, comp.id, component=comp)
                hit = ink[3] > ty + 1.0 and ink[2] > tx + 0.2 and ink[0] < tx + tw - 0.2
            if not hit:
                continue
            findings.append(
                Finding(
                    "symbol_on_title_block",
                    "warning",
                    f"{sheet.id}:{comp.id} sits on the title block",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _panel_enclosure_findings(project: Project, catalog: Catalog) -> list[Finding]:
    """A panel backpanel needs a catalog box. A chosen box that the parts miss stays chosen."""

    from panelcad.core.layout import enclosure_is_sized, footprint_size
    from panelcad.core.panels import panel_backpanel_chosen, panel_tag
    from panelcad.library.panel import effective_footprint, is_incabinet

    findings: list[Finding] = []
    for panel in project.panels:
        sheet = next(
            (
                item
                for item in project.sheets
                if item.kind == "panel_layout" and item.panel == panel.id
            ),
            None,
        )
        if sheet is None:
            continue
        tag = panel_tag(panel)
        if not enclosure_is_sized(sheet.enclosure, sheet.page):
            findings.append(
                Finding(
                    "panel_needs_enclosure",
                    "warning",
                    f"{tag} has no enclosure. Pick a catalog box or run Size Enclosure.",
                    sheet=sheet.id,
                    ref=tag,
                )
            )
            continue
        if not panel_backpanel_chosen(project, sheet) or sheet.enclosure is None:
            continue
        enc = sheet.enclosure
        scale = float(enc.draw_scale or 1.0) or 1.0
        right = enc.x + enc.width_mm * scale
        bottom = enc.y + enc.height_mm * scale
        outside = False
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            if not is_incabinet(device, comp) and effective_footprint(device, comp).mount in {"none", "field"}:
                continue
            width, height = footprint_size(device, comp)
            if width <= 0 and height <= 0:
                continue
            if (
                comp.x + 0.05 < enc.x
                or comp.y + 0.05 < enc.y
                or comp.x + width - 0.05 > right
                or comp.y + height - 0.05 > bottom
            ):
                outside = True
                break
        stored = project.properties.get("panel_unplaced") or {}
        pending: list[str] = []
        if isinstance(stored, dict):
            raw = stored.get(sheet.id) or []
            if isinstance(raw, dict):
                owners = [str(ref) for ref in raw.get("owners") or [] if str(ref)]
                raw = raw.get("refs") or []
                if owners:
                    alive = {comp.id for item in project.sheets for comp in item.components}
                    if not any(ref in alive for ref in owners):
                        raw = []
            if isinstance(raw, (list, tuple)):
                present = {comp.id for comp in sheet.components}
                pending = [str(ref) for ref in raw if str(ref) and str(ref) not in present]
        if not outside and not pending:
            continue
        name = str(enc.catalog or enc.name or panel.enclosure_id or tag).strip()
        if pending:
            names = ", ".join(pending)
            verb = "does" if len(pending) == 1 else "do"
            message = (
                f"{tag} enclosure {name} does not fit the circuit. "
                f"{names} {verb} not fit in the free backplate. "
                "Run Size Enclosure to choose a larger box."
            )
        else:
            message = (
                f"{tag} enclosure {name} does not fit the circuit. "
                "Run Size Enclosure to choose a larger box."
            )
        findings.append(
            Finding(
                "panel_enclosure_tight",
                "warning",
                message,
                sheet=sheet.id,
                ref=tag,
                target=str(panel.enclosure_id or ""),
            )
        )
    return findings


def _outside_enclosure(project: Project, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        if not is_layout_sheet(sheet) or sheet.enclosure is None:
            continue
        enc = sheet.enclosure
        scale = float(enc.draw_scale or 1.0) or 1.0
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            if not is_incabinet(device, comp) and effective_footprint(device, comp).mount in {"none", "field"}:
                continue
            w, h = footprint_size(device, comp)
            if w <= 0 and h <= 0:
                continue
            ox, oy = comp.x, comp.y
            if (
                ox + 0.05 < enc.x
                or oy + 0.05 < enc.y
                or ox + w - 0.05 > enc.x + enc.width_mm * scale
                or oy + h - 0.05 > enc.y + enc.height_mm * scale
            ):
                place = "door" if sheet.kind == "door_layout" else "enclosure"
                findings.append(
                    Finding(
                        "outside_enclosure",
                        "warning",
                        f"{sheet.id}:{comp.id} does not fit the {place}",
                        sheet=sheet.id,
                        ref=comp.id,
                        x=comp.x,
                        y=comp.y,
                    )
                )
    return findings


def _xref_findings(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.core.xref import xref_problems

    findings: list[Finding] = []
    for item in xref_problems(project, catalog):
        findings.append(
            Finding(
                str(item["check"]),
                item["severity"],  # type: ignore[arg-type]
                str(item["message"]),
                sheet=str(item.get("sheet") or ""),
                ref=str(item.get("ref") or ""),
                target=str(item.get("target") or ""),
                x=item.get("x"),
                y=item.get("y"),
            )
        )
    return findings


def _sizing_options(project: Project):
    from panelcad.core.enclosure import options_from_defaults
    from panelcad.library.enclosures import load_enclosures

    stored = project.properties.get("enclosure_sizing") or {}
    defaults = load_enclosures().defaults
    if isinstance(stored, dict):
        merged = dict(defaults)
        merged.update(stored)
        return options_from_defaults(merged)
    return options_from_defaults(defaults)


def _builtin_parts():
    cached = getattr(_builtin_parts, "cached", None)
    if cached is None:
        from panelcad.library.parts import default_parts_catalog

        cached = default_parts_catalog()
        _builtin_parts.cached = cached
    return cached


def _thermal_limit_findings(project: Project, catalog: Catalog) -> list[Finding]:
    """Component max-ambient and sealed-enclosure closed-loop checks from the thermal report."""

    from panelcad.core.thermal import compute_heat_load

    report = compute_heat_load(project, catalog, _builtin_parts())
    findings: list[Finding] = []
    for item in report.get("findings") or []:
        findings.append(
            Finding(
                str(item.get("check") or ""),
                "warning",
                str(item.get("message") or ""),
                sheet=str(item.get("sheet") or ""),
                ref=str(item.get("ref") or ""),
                x=item.get("x"),
                y=item.get("y"),
            )
        )
    return findings


def _heat_rise_over_limit(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.core.enclosure import collect_parts, heat_report

    sheet, enc = None, None
    for item in project.sheets:
        if item.kind == "panel_layout" and item.enclosure is not None:
            sheet, enc = item, item.enclosure
            break
    if enc is None:
        return []
    options = _sizing_options(project)
    cabinet, _door = collect_parts(project, catalog)
    if not cabinet:
        return []
    heat = heat_report(cabinet, None, options, enclosure=enc)
    if heat.get("ok") or heat.get("rise_k") is None:
        return []
    return [
        Finding(
            "heat_rise_over_limit",
            "warning",
            (
                f"{sheet.id if sheet else 'panel'}: temperature rise {heat['rise_k']:g} K "
                f"from {heat['watts_known']:g} W exceeds the {heat['allowed_rise_k']:g} K limit "
                f"(ambient {heat['ambient_c']:g} °C, max {heat['max_internal_c']:g} °C)"
            ),
            sheet=sheet.id if sheet else "",
            ref=enc.name or "enclosure",
            x=enc.x,
            y=enc.y,
        )
    ]


def _soft_starter_without_overload(project: Project) -> list[Finding]:
    """A soft-starter motor with no overload device must say the starter protects it."""

    findings: list[Finding] = []
    for sheet in project.sheets:
        motors = []
        overloads = 0
        soft = False
        for comp in sheet.components:
            role = str(comp.properties.get("role") or "")
            if role == "softstarter":
                soft = True
            elif role == "motor":
                motors.append(comp)
            elif role == "overload":
                overloads += 1
        if not soft:
            continue
        plain = [
            comp
            for comp in motors
            if "soft starter internal" not in str(comp.properties.get("description") or "").lower()
        ]
        if len(plain) <= overloads:
            continue
        for comp in plain[overloads:]:
            findings.append(
                Finding(
                    "soft_starter_overload",
                    "warning",
                    (
                        f"{comp.id} has no overload. If the soft starter's built-in overload "
                        "protects this motor, the description should say "
                        "'overload: soft starter internal'."
                    ),
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return findings


def _missing_footprint(project: Project, catalog: Catalog) -> list[Finding]:
    from panelcad.core.enclosure import collect_parts

    findings: list[Finding] = []
    loc: dict[str, tuple[str, float, float]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            loc.setdefault(comp.id, (sheet.id, comp.x, comp.y))
    cabinet, door = collect_parts(project, catalog)
    seen: set[str] = set()
    for part in (*cabinet, *door):
        if not part.missing_size or part.ref in seen:
            continue
        seen.add(part.ref)
        sheet_id, x, y = loc.get(part.ref, ("", 0.0, 0.0))
        findings.append(
            Finding(
                "missing_footprint",
                "info",
                f"{part.ref} has no library width/height for enclosure sizing",
                sheet=sheet_id,
                ref=part.ref,
                x=x,
                y=y,
            )
        )
    return findings


def _cable_shield_findings(project: Project) -> list[Finding]:
    """Shield practice a shop can check without opening the cable editor.

    A shielded cable with neither end grounded is a warning. An instrument
    shield grounded at both ends is a warning. The landings themselves come
    from the cable; this does not invent a bond.
    """

    from panelcad.core.cables import shield_end_grounded

    findings: list[Finding] = []
    for cable in project.cables:
        if not cable.shield and not cable.drain:
            continue
        drains = [
            item
            for item in cable.assignments
            if item.shield or item.role == "shield" or item.id == "SH"
        ]
        if not drains:
            panel_grounded = False
            field_grounded = False
        else:
            panel_grounded = any(shield_end_grounded(item.panel_terminal) for item in drains)
            field_grounded = any(shield_end_grounded(item.field_terminal) for item in drains)
        if not panel_grounded and not field_grounded:
            findings.append(
                Finding(
                    "shield_ungrounded",
                    "warning",
                    f"{cable.tag} shield is not grounded at either end",
                    ref=cable.tag,
                    target="SH",
                )
            )
        if (cable.to_kind or "").lower() == "instrument" and panel_grounded and field_grounded:
            findings.append(
                Finding(
                    "instrument_shield_both_ends",
                    "warning",
                    f"{cable.tag} instrument shield is grounded at both ends",
                    ref=cable.tag,
                    target="SH",
                )
            )
    return findings
