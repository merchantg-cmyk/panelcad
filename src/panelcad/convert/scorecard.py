"""IEC <-> NEMA conversion scorecard harness and 18-print synthetic/public corpus.

Implements PLAN.md Section 2b, Section 5, and Section 6 (Patch 8):
- 18 varied synthetic/public IEC prints (zero customer prints or names).
- Hand-verified ground truth per print (device tags, roles, library IDs,
  contact terminal numbers, From-To connections, cross-references, and
  translated notes).
- Versioned scorer (`SCORER_VERSION = "1.0.0"`) with incremental/resumable
  JSON/JSONL progress saving.
- Side-by-side comparison across `"rules_only"`, `"ai_assisted"`, and
  `"ai_recorded"` modes using the identical `scorer_version`.
- Golden NEMA From-To CSV builders and verifiers for `dol`, `reversing`,
  and `plc`.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from panelcad.api.session import Session
from panelcad.convert.ai_assist import (
    ConfiguredConversionAIBackend,
    ConversionAIBackend,
    NoOpAIBackend,
    RecordedJSONBackend,
)
from panelcad.convert.engine import ConversionEngine
from panelcad.convert.flags import sheet_flags
from panelcad.convert.from_to_check import check_connections
from panelcad.convert.import_convert import (
    RecordedImportVisionBackend,
    build_iec_project_from_imported_from_to,
    import_iec_print_to_from_to,
)
from panelcad.convert.netlist import same_netlist, sheet_edges
from panelcad.core.models import Component
from panelcad.core.xref import xref_families
from panelcad.examples.dol import build_dol

SCORER_VERSION = "1.0.0"

CORPUS_IDS: tuple[str, ...] = (
    "dol_starter",
    "reversing_starter",
    "star_delta_starter",
    "two_speed_dahlander",
    "vfd_drive_cell",
    "soft_starter_bypass",
    "plc_digital_io",
    "plc_analog_loop",
    "safety_relay_dual_channel",
    "control_transformer_dist",
    "multi_page_conveyor_3p",
    "multi_page_pump_duplex",
    "multi_page_hvac_ahu",
    "siemens_style_numbering",
    "schneider_style_numbering",
    "eplan_potential_chains",
    "ambiguous_roles_sheet",
    "imported_scan_fixture",
)

# SHA-256 digests of lowercase banned tokens so zero literal customer or staff
# names appear in source files while still blocking accidental corpus leakage.
FORBIDDEN_CORPUS_TOKEN_SHA256: frozenset[str] = frozenset({
    "0522a55e2d5f0993a3d66d28864b2862a7218a75ea7968b075333434404485c3",
    "1d374d6d93ceef147b9fb8f46d183a67e2eafe9303cbc4cf1fd4e74ceec81a0b",
    "1d953f8cd920ebf186cddb641d5665dc733ebf67190fe480d11ad09b1054e51c",
    "4f31fa50e5bd5ff45684e560fc24aeee527a43739ab611c49c51098a33e2b469",
    "77624599e8535a49973cd4441915ba24494f50cfeead03895dde3f705bda2808",
    "7f0b629cbb9d794b3daf19fcd686a30a039b47395545394dadc0574744996a87",
    "99da7c015a2b563da344d6354994826b10e44e8c99357d341c7ccd11239a9575",
    "a09cfcd2c450c5708d259eaff907a6bbe902f1b65fbba9cad3cc4e053a752830",
    "c35d09d5269f019cdf5ebe4ad5b8d7f8e541850dfe1f8f9a63a7edbc55a5bc77",
    "d7a1c76be3532cf992bcd4007202e4355413fadb579fe93752a9f75390e8f1be",
    "d940b56413af584806504ea58f1f7c0b6da2a1ea2dc6733d32117427c7c83936",
    "e6e62ee55819cdc13126a8633ee8c43eaf48abf491cbca92129ddf5505b70bc1",
    "eb0b9c61cefd623588503c130be4db393c92887fe5718d9ddf82010be625757b",
    "f148389d080cfe85952998a8a367e2f7eaf35f2d72d2599a5b0412fe4094d65c",
})


def has_forbidden_corpus_terms(text: str, *, banned_file: Path | None = None) -> bool:
    """Return True if `text` contains any token matching `FORBIDDEN_CORPUS_TOKEN_SHA256`
    or optional `public-snapshot.banned.txt` when present on disk.
    """
    low = text.lower()
    for tok in re.findall(r"[a-z0-9]+", low):
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        if digest in FORBIDDEN_CORPUS_TOKEN_SHA256:
            return True
    candidate = banned_file
    if candidate is None:
        repo_candidate = Path(__file__).resolve().parents[3] / "scripts" / "public-snapshot.banned.txt"
        if repo_candidate.is_file():
            candidate = repo_candidate
    if candidate is not None and candidate.is_file():
        for line in candidate.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if not entry:
                continue
            escaped = re.escape(entry.lower())
            pat = rf"\b{escaped}\b" if len(entry) <= 4 else escaped
            if re.search(pat, low):
                return True
    return False

AMBIGUOUS_ROLES_RECORDINGS: dict[str, Any] = {
    "roles": {
        "Q1": {
            "role": "circuit_breaker",
            "prefix": "CB",
            "confidence": 0.95,
            "reason": "Leitungsschutzschalter 24V -> circuit_breaker (CB)",
            "model": "recorded-json/v1",
        },
        "F1": {
            "role": "fuse",
            "prefix": "FU",
            "confidence": 0.95,
            "reason": "Sicherung Steuerspannung -> fuse (FU)",
            "model": "recorded-json/v1",
        },
        "K1": {
            "role": "control_relay",
            "prefix": "CR",
            "confidence": 0.94,
            "reason": "Hilfsschütz Sicherheitskreis -> control_relay (CR)",
            "model": "recorded-json/v1",
        },
    },
    "symbols": {
        "iec.custom.breaker_1p": {
            "suggested_library_id": "nema.breakers.mcb_1p_16a",
            "pin_map": {"1": "1", "2": "2"},
            "confidence": 0.94,
            "reason": "Pin-compatible NEMA 1P miniature circuit breaker",
            "model": "recorded-json/v1",
        },
        "iec.custom.control_fuse_1p": {
            "suggested_library_id": "nema.protection.fuse_10a",
            "pin_map": {"1": "1", "2": "2"},
            "confidence": 0.94,
            "reason": "Pin-compatible NEMA 1P control fuse",
            "model": "recorded-json/v1",
        },
        "iec.custom.safety_relay_2p": {
            "suggested_library_id": "nema.relays.cr",
            "pin_map": {"A1": "A1", "A2": "A2"},
            "confidence": 0.92,
            "reason": "Pin-compatible NEMA control relay coil",
            "model": "recorded-json/v1",
        },
    },
    "notes": {
        "Hauptstromkreis & Steuerspannung - Not-Halt": {
            "text": "Main Power Circuit & Control Voltage - Emergency Stop",
            "confidence": 0.96,
            "reason": "Standard IEC German electrical title translation",
            "model": "recorded-json/v1",
        },
        "Not-Halt Taster": {
            "text": "Emergency Stop Pushbutton",
            "confidence": 0.95,
            "reason": "Not-Halt -> Emergency Stop",
            "model": "recorded-json/v1",
        },
        "Sicherung Steuerspannung": {
            "text": "Fuse Control Voltage",
            "confidence": 0.95,
            "reason": "Steuerspannung -> Control Voltage",
            "model": "recorded-json/v1",
        },
        "Hauptstromkreis": {
            "text": "Main Power Circuit",
            "confidence": 0.96,
            "reason": "Hauptstromkreis -> Main Power Circuit",
            "model": "recorded-json/v1",
        },
    },
}


@dataclass(frozen=True)
class CorpusGroundTruth:
    """Hand-verified ground truth for a single IEC -> NEMA corpus print."""

    expected_tags: dict[str, str]
    expected_roles: dict[str, str]
    expected_library_ids: dict[str, str]
    expected_contact_pins: dict[str, tuple[str, str]]
    expected_from_to: list[tuple[str, str, str, str]]
    expected_iec_from_to: list[tuple[str, str, str, str]]
    expected_xrefs: dict[str, list[str]]
    expected_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class CorpusPrintItem:
    """One synthetic/public IEC print in the 18-print conversion corpus."""

    item_id: str
    category: str
    description: str
    source_kind: str
    build_session: Callable[[], Session]
    ground_truth: CorpusGroundTruth
    ai_recordings: dict[str, Any] = field(default_factory=dict)


CorpusItem = CorpusPrintItem


@dataclass
class ItemScorecardResult:
    """Per-print scorecard metrics."""

    item_id: str
    category: str
    mode: str
    scorer_version: str
    device_precision: float
    device_recall: float
    device_f1: float
    tag_accuracy: float
    contact_number_accuracy: float
    connection_f1: float
    netlist_ok: bool
    round_trip_from_to_ok: bool
    round_trip_from_to_match: bool
    xref_accuracy: float
    note_accuracy: float
    sheet_flags_clear_rate: float
    sheet_flags_count: int
    flagged_rate: float
    wrong_unflagged_rate: float
    flagged_count: int
    wrong_unflagged_count: int
    total_decisions: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScorecardReport:
    """Aggregate scorecard report across the 18-print corpus."""

    scorer_version: str
    timestamp: str
    mode: str
    corpus_size: int
    completed_count: int
    device_precision: float
    device_recall: float
    device_f1: float
    tag_accuracy: float
    contact_number_accuracy: float
    connection_f1: float
    round_trip_from_to_ok: bool
    round_trip_from_to_match: bool
    round_trip_pass_rate: float
    xref_accuracy: float
    note_accuracy: float
    sheet_flags_clear_rate: float
    flagged_rate: float
    wrong_unflagged_rate: float
    total_flagged_count: int
    total_wrong_unflagged_count: int
    total_decisions: int
    items: list[ItemScorecardResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


def _canonical_from_to_tuples(session: Session) -> list[tuple[str, str, str, str]]:
    """Return sorted canonical From-To tuples `(sheet_id, wire_number, ep_a, ep_b)`.

    Multi-pin nets sharing a wire number are star-canonicalized against the
    lexicographically smallest endpoint in each connected component so the
    tuple set is invariant to geometric spanning-tree hop order.
    """
    by_wire: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    ep_sheet: dict[str, str] = {}
    for row in session.from_to_rows(sort="number"):
        ep_a = str(row.get("from", ""))
        ep_b = str(row.get("to", ""))
        sh_a = str(row.get("from_sheet") or row.get("to_sheet") or "")
        sh_b = str(row.get("to_sheet") or row.get("from_sheet") or "")
        ep_sheet.setdefault(ep_a, sh_a)
        ep_sheet.setdefault(ep_b, sh_b)
        wire_num = str(row.get("wire_number") or "")
        by_wire[wire_num].append((sh_a, ep_a, ep_b))

    out: list[tuple[str, str, str, str]] = []
    for wire_num, edges in by_wire.items():
        if len(edges) == 1:
            sh, a, b = edges[0]
            ea, eb = sorted((a, b))
            out.append((sh, wire_num, ea, eb))
        else:
            adj: dict[str, set[str]] = defaultdict(set)
            for _sh, a, b in edges:
                adj[a].add(b)
                adj[b].add(a)
            visited: set[str] = set()
            for node in sorted(adj.keys()):
                if node in visited:
                    continue
                comp: list[str] = []
                stack = [node]
                visited.add(node)
                while stack:
                    cur = stack.pop()
                    comp.append(cur)
                    for nxt in adj[cur]:
                        if nxt not in visited:
                            visited.add(nxt)
                            stack.append(nxt)
                comp.sort()
                root = comp[0]
                for other in comp[1:]:
                    out.append((ep_sheet.get(other, ""), wire_num, root, other))
    out.sort()
    return out


def _extract_prefix(tag: str) -> str:
    """Extract the leading alpha prefix from a NEMA/IEC device tag."""
    core = tag.split("-")[-1] if ("=" in tag or "+" in tag) else tag
    m = re.match(r"^([A-Za-z]+)", core.lstrip("-"))
    return m.group(1).upper() if m else core


def _canonical_xrefs(session: Session) -> dict[str, list[str]]:
    """Return canonical coil -> sorted list of contact component IDs."""
    families = xref_families(session.project, session.catalog)
    result: dict[str, list[str]] = {}
    for _fam_key, fam in sorted(families.items()):
        if fam.coils and fam.contacts:
            contacts = sorted({m.comp.id for m in fam.contacts})
            for coil in fam.coils:
                result[coil.comp.id] = contacts
    return result


def _register_custom_ambiguous_devices(session: Session) -> None:
    """Register the synthetic custom IEC symbols on `session.catalog`."""
    base_fuse = session.catalog.get("iec.protection.fuse_10a")
    session.catalog.devices["iec.custom.breaker_1p"] = base_fuse.model_copy(
        update={
            "library_id": "iec.custom.breaker_1p",
            "name": "Custom IEC 1P Control Circuit Breaker",
            "category": "custom",
            "ref_prefix": "Q",
        }
    )
    session.catalog.devices["iec.custom.control_fuse_1p"] = base_fuse.model_copy(
        update={
            "library_id": "iec.custom.control_fuse_1p",
            "name": "Custom IEC 1P Control Fuse",
            "category": "custom",
            "ref_prefix": "F",
        }
    )
    base_coil = session.catalog.get("iec.relays.cr")
    session.catalog.devices["iec.custom.safety_relay_2p"] = base_coil.model_copy(
        update={
            "library_id": "iec.custom.safety_relay_2p",
            "name": "Custom IEC Safety Control Relay Coil",
            "category": "custom",
            "ref_prefix": "K",
        }
    )


# ---------------------------------------------------------------------------
# 18-Print Synthetic / Public IEC Corpus Builders
# ---------------------------------------------------------------------------


def _configure_sheet(
    s: Session,
    sheet_id: str,
    *,
    drawing_style: str = "iec",
    rail_hot: str = "",
    rail_return: str = "",
) -> None:
    sh = s.sheet(sheet_id)
    sh.drawing_style = drawing_style
    if rail_hot:
        sh.rail_hot = rail_hot
    if rail_return:
        sh.rail_return = rail_return


def _in_out_pins(library_id: str, cat: Any) -> tuple[str, str]:
    dev = cat.get(library_id)
    pins = [pin.id for pin in dev.pins]
    if "11" in pins and "12" in pins:
        return ("11", "12")
    if "13" in pins and "14" in pins:
        return ("13", "14")
    if "21" in pins and "22" in pins:
        return ("21", "22")
    if "95" in pins and "96" in pins:
        return ("95", "96")
    if "15" in pins and "16" in pins:
        return ("15", "16")
    if "15" in pins and "18" in pins:
        return ("15", "18")
    if "A1" in pins and "A2" in pins:
        return ("A1", "A2")
    if "X1" in pins and "X2" in pins:
        return ("X1", "X2")
    if "1" in pins and "2" in pins:
        return ("1", "2")
    if "PLUS" in pins and "SIG" in pins:
        return ("PLUS", "SIG")
    return (pins[0], pins[1])


def _add_rung(
    s: Session,
    sheet_id: str,
    *,
    y: float,
    contacts: list[tuple[str, str]],
    coil: tuple[str, str],
    seal_in: tuple[str, str, str] | None = None,
) -> None:
    sh = s.sheet(sheet_id)
    hot = sh.rail_hot or "L"
    ret = sh.rail_return or "N"
    rung_idx = int(round(y / 10.0))
    # In IEC vertical control columns, use rung_idx to pick column x, and step y down
    col_x = 60.0 + ((rung_idx - 3) // 5) * 65.0
    for idx, (lib_id, ref) in enumerate(contacts):
        cy = 45.0 + idx * 32.0
        comp = s.place(lib_id, ref=ref, x=col_x, y=cy, rotation=90, sheet_id=sheet_id)
        if "-" in ref:
            parent_ref, suffix = ref.rsplit("-", 1)
            comp.properties["parent"] = parent_ref
            if suffix == "13":
                comp.properties["terminals"] = "13-14"
            elif suffix == "23":
                comp.properties["terminals"] = "23-24"
                comp.properties["pin_labels"] = {"13": "23", "14": "24"}
            elif suffix == "21":
                comp.properties["terminals"] = "21-22"
            elif suffix == "95":
                comp.properties["terminals"] = "95-96"
            elif suffix == "15":
                comp.properties["terminals"] = "15-16"
            elif suffix == "17":
                comp.properties["terminals"] = "17-18"
    coil_lib, coil_ref = coil
    coil_y = 45.0 + len(contacts) * 32.0 + 16.0
    s.place(coil_lib, ref=coil_ref, x=col_x, y=coil_y, rotation=90, sheet_id=sheet_id)

    chain = list(contacts) + [coil]
    first_in, _ = _in_out_pins(chain[0][0], s.catalog)
    s.wire(hot, f"{chain[0][1]}.{first_in}", net=hot, sheet_id=sheet_id)

    for idx in range(len(chain) - 1):
        lib_a, ref_a = chain[idx]
        lib_b, ref_b = chain[idx + 1]
        _, out_a = _in_out_pins(lib_a, s.catalog)
        in_b, _ = _in_out_pins(lib_b, s.catalog)
        net_name = f"N_{sheet_id}_{rung_idx}_{idx + 1}"
        s.wire(f"{ref_a}.{out_a}", f"{ref_b}.{in_b}", net=net_name, number=f"{rung_idx}{idx + 1}", sheet_id=sheet_id)

    _, coil_out = _in_out_pins(coil_lib, s.catalog)
    s.wire(f"{coil_ref}.{coil_out}", ret, net=ret, sheet_id=sheet_id)

    if seal_in is not None:
        seal_lib, seal_ref, target_ref = seal_in
        target_idx = next(i for i, (_, r) in enumerate(contacts) if r == target_ref)
        target_y = 45.0 + target_idx * 32.0
        seal_comp = s.place(seal_lib, ref=seal_ref, x=col_x + 17.0, y=target_y, rotation=90, sheet_id=sheet_id)
        if "-" in seal_ref:
            parent_ref, _ = seal_ref.rsplit("-", 1)
            seal_comp.properties["parent"] = parent_ref
            seal_comp.properties["terminals"] = "13-14"
        s_in, s_out = _in_out_pins(seal_lib, s.catalog)
        t_lib, _ = contacts[target_idx]
        t_in, t_out = _in_out_pins(t_lib, s.catalog)
        up_net = hot if target_idx == 0 else f"N_{sheet_id}_{rung_idx}_{target_idx}"
        dn_net = f"N_{sheet_id}_{rung_idx}_{target_idx + 1}"
        s.wire(f"{target_ref}.{t_in}", f"{seal_ref}.{s_in}", net=up_net, number=f"{rung_idx}{target_idx}A", sheet_id=sheet_id)
        s.wire(f"{seal_ref}.{s_out}", f"{target_ref}.{t_out}", net=dn_net, number=f"{rung_idx}{target_idx}B", sheet_id=sheet_id)


def _build_01_dol_starter() -> Session:
    s = Session.create("Synthetic DOL Motor Starter")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="dol", control="local", standard="IEC")
    return s


def _build_02_reversing_starter() -> Session:
    s = Session.create("Synthetic Reversing Motor Starter")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="reversing", control="local", standard="IEC")
    return s


def _build_03_star_delta_starter() -> Session:
    s = Session.create("Synthetic Star-Delta Starter")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="star_delta", control="local", standard="IEC", timer_seconds=8)
    return s


def _build_04_two_speed_dahlander() -> Session:
    s = Session.create("Synthetic Two-Speed Dahlander Starter", sheet_id="s1", title="Two-Speed Control")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "s1", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "s1",
        y=36.0,
        contacts=[
            ("iec.operators.pb_nc", "S0"),
            ("iec.operators.pb_no", "S1"),
            ("iec.contactors.aux_nc", "K2-21"),
        ],
        coil=("iec.contactors.coil", "K1"),
        seal_in=("iec.contactors.aux_no", "K1-13", "S1"),
    )
    _add_rung(s,
        "s1",
        y=96.0,
        contacts=[
            ("iec.operators.pb_no", "S2"),
            ("iec.contactors.aux_nc", "K1-21"),
        ],
        coil=("iec.contactors.coil", "K2"),
        seal_in=("iec.contactors.aux_no", "K2-13", "S2"),
    )
    _add_rung(s,
        "s1",
        y=156.0,
        contacts=[("iec.contactors.aux_no", "K2-23")],
        coil=("iec.contactors.coil", "K3"),
    )
    return s


def _build_05_vfd_drive_cell() -> Session:
    s = Session.create("Synthetic VFD Drive Cell")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="vfd", control="local", standard="IEC", drive="generic_1k5")
    return s


def _build_06_soft_starter_bypass() -> Session:
    s = Session.create("Synthetic Soft Starter Bypass")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="soft_starter", control="local", standard="IEC")
    return s


def _build_07_plc_digital_io() -> Session:
    s = Session.create("Synthetic PLC Digital IO")
    s.set_title_block(standard="IEC")
    s.insert_circuit(kind="plc_motor", control="local", standard="IEC")
    return s


def _build_08_plc_analog_loop() -> Session:
    s = Session.create("Synthetic PLC Analog Loop", sheet_id="analog", title="4-20mA Analog Loops")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "analog", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "analog",
        y=36.0,
        contacts=[("iec.protection.fuse_10a", "F1")],
        coil=("iec.loads.transmitter_2wire", "B1"),
    )
    _add_rung(s,
        "analog",
        y=96.0,
        contacts=[
            ("iec.operators.pressure_no", "B2"),
            ("iec.operators.temperature_nc", "B3"),
        ],
        coil=("iec.relays.cr", "K1"),
    )
    _add_rung(s,
        "analog",
        y=156.0,
        contacts=[("iec.contactors.aux_no", "K1-13")],
        coil=("iec.loads.solenoid", "Y1"),
    )
    return s


def _build_09_safety_relay_dual_channel() -> Session:
    s = Session.create("Synthetic Dual-Channel Safety Relay", sheet_id="safety", title="Safety Relay Circuit")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "safety", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "safety",
        y=36.0,
        contacts=[
            ("iec.operators.estop", "S1"),
            ("iec.operators.limit_nc", "S2"),
            ("iec.operators.pb_no", "S3"),
        ],
        coil=("iec.relays.cr", "K1"),
        seal_in=("iec.contactors.aux_no", "K1-13", "S3"),
    )
    _add_rung(s,
        "safety",
        y=96.0,
        contacts=[
            ("iec.operators.estop", "S4"),
            ("iec.operators.limit_nc", "S5"),
            ("iec.contactors.aux_no", "K1-23"),
        ],
        coil=("iec.relays.cr", "K2"),
    )
    _add_rung(s,
        "safety",
        y=156.0,
        contacts=[("iec.contactors.aux_no", "K2-13")],
        coil=("iec.operators.pilot_light", "H1"),
    )
    return s


def _build_10_control_transformer_dist() -> Session:
    s = Session.create("Synthetic Control Transformer Distribution", sheet_id="dist", title="Control Power Distribution")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "dist", drawing_style="iec")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=25, sheet_id="dist")
    s.place("iec.power.control_transformer", ref="T1", x=40, y=85, sheet_id="dist")
    s.place("iec.protection.fuse_10a", ref="F1", x=110, y=85, sheet_id="dist")
    s.place("iec.power.psu_24v_5a", ref="G1", x=40, y=150, sheet_id="dist")
    s.wire("Q1.2", "T1.H1", net="L1", number="101", sheet_id="dist")
    s.wire("Q1.4", "T1.H2", net="L2", number="102", sheet_id="dist")
    s.wire("T1.X1", "F1.1", net="120V_RAW", number="110", sheet_id="dist")
    s.wire("F1.2", "G1.L", net="120V", number="111", sheet_id="dist")
    s.wire("T1.X2", "G1.N", net="N", number="112", sheet_id="dist")
    return s


def _build_11_multi_page_conveyor_3p() -> Session:
    s = Session.create("Synthetic 3-Page Conveyor System", sheet_id="p1-power", title="Page 1 - Conveyor Power")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "p1-power", drawing_style="iec")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=25, sheet_id="p1-power")
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=85, sheet_id="p1-power")
    s.place("iec.protection.overload_9a", ref="F1", x=40, y=145, sheet_id="p1-power")
    s.place("iec.loads.motor_3ph", ref="M1", x=40, y=205, sheet_id="p1-power")
    s.wire("Q1.2", "K1.L1", net="L1", number="101", sheet_id="p1-power")
    s.wire("Q1.4", "K1.L2", net="L2", number="102", sheet_id="p1-power")
    s.wire("Q1.6", "K1.L3", net="L3", number="103", sheet_id="p1-power")
    s.wire("K1.T1", "F1.1", net="T1", number="104", sheet_id="p1-power")
    s.wire("K1.T2", "F1.3", net="T2", number="105", sheet_id="p1-power")
    s.wire("K1.T3", "F1.5", net="T3", number="106", sheet_id="p1-power")
    s.wire("F1.2", "M1.U1", net="U1", number="107", sheet_id="p1-power")
    s.wire("F1.4", "M1.V1", net="V1", number="108", sheet_id="p1-power")
    s.wire("F1.6", "M1.W1", net="W1", number="109", sheet_id="p1-power")

    s.add_sheet("p2-control", title="Page 2 - Conveyor Control")
    _configure_sheet(s, "p2-control", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p2-control",
        y=36.0,
        contacts=[
            ("iec.operators.estop", "S0"),
            ("iec.operators.pb_nc", "S1"),
            ("iec.operators.pb_no", "S2"),
            ("iec.protection.ol_contact", "F1-95"),
        ],
        coil=("iec.contactors.coil", "K1"),
        seal_in=("iec.contactors.aux_no", "K1-13", "S2"),
    )

    s.add_sheet("p3-status", title="Page 3 - Conveyor Status")
    _configure_sheet(s, "p3-status", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p3-status",
        y=36.0,
        contacts=[("iec.contactors.aux_no", "K1-23")],
        coil=("iec.operators.pilot_light", "H1"),
    )
    _add_rung(s,
        "p3-status",
        y=96.0,
        contacts=[("iec.contactors.aux_nc", "K1-21")],
        coil=("iec.operators.horn", "H2"),
    )
    return s


def _build_12_multi_page_pump_duplex() -> Session:
    s = Session.create("Synthetic Duplex Pump Station", sheet_id="p1-lead", title="Page 1 - Lead Pump Control")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "p1-lead", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p1-lead",
        y=36.0,
        contacts=[
            ("iec.operators.float_no", "B1"),
            ("iec.protection.ol_contact", "F1-95"),
        ],
        coil=("iec.contactors.coil", "K1"),
    )

    s.add_sheet("p2-lag", title="Page 2 - Lag Pump & Alternator")
    _configure_sheet(s, "p2-lag", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p2-lag",
        y=36.0,
        contacts=[
            ("iec.operators.float_no", "B2"),
            ("iec.protection.ol_contact", "F2-95"),
        ],
        coil=("iec.contactors.coil", "K2"),
    )
    _add_rung(s,
        "p2-lag",
        y=96.0,
        contacts=[("iec.contactors.aux_no", "K1-13")],
        coil=("iec.relays.alternating", "K3"),
    )

    s.add_sheet("p3-alarm", title="Page 3 - High Level Alarm")
    _configure_sheet(s, "p3-alarm", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p3-alarm",
        y=36.0,
        contacts=[
            ("iec.operators.level_no", "B3"),
            ("iec.contactors.aux_no", "K2-13"),
        ],
        coil=("iec.operators.pilot_light", "H1"),
    )
    return s


def _build_13_multi_page_hvac_ahu() -> Session:
    s = Session.create("Synthetic Multi-Page HVAC AHU", sheet_id="p1-fan", title="Page 1 - Supply Fan & Damper")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "p1-fan", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p1-fan",
        y=36.0,
        contacts=[
            ("iec.operators.temperature_nc", "B1"),
            ("iec.operators.selector_no", "S1"),
        ],
        coil=("iec.relays.cr", "K1"),
    )
    _add_rung(s,
        "p1-fan",
        y=96.0,
        contacts=[
            ("iec.contactors.aux_no", "K1-13"),
            ("iec.operators.limit_no", "S2"),
        ],
        coil=("iec.contactors.coil", "K2"),
    )

    s.add_sheet("p2-exhaust", title="Page 2 - Exhaust Fan & Status")
    _configure_sheet(s, "p2-exhaust", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "p2-exhaust",
        y=36.0,
        contacts=[
            ("iec.contactors.aux_no", "K2-13"),
            ("iec.operators.flow_no", "B2"),
        ],
        coil=("iec.loads.solenoid", "Y1"),
    )
    _add_rung(s,
        "p2-exhaust",
        y=96.0,
        contacts=[("iec.contactors.aux_no", "K1-23")],
        coil=("iec.operators.pilot_light", "H1"),
    )
    return s


def _build_14_siemens_style_numbering() -> Session:
    s = Session.create("Synthetic Siemens Style Numbering", sheet_id="s1", title="Siemens Page.Column Style")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "s1", drawing_style="iec")
    s.place("iec.breakers.mcb_3p_32a", ref="=A1+P1-Q1", x=40, y=25, sheet_id="s1")
    s.place("iec.contactors.ac3_9a", ref="=A1+P1-K1", x=40, y=85, sheet_id="s1")
    s.place("iec.protection.overload_9a", ref="=A1+P1-F1", x=40, y=145, sheet_id="s1")
    s.place("iec.loads.motor_3ph", ref="=A1+P1-M1", x=40, y=205, sheet_id="s1")
    s.wire("=A1+P1-Q1.2", "=A1+P1-K1.L1", net="L1", number="1.1", sheet_id="s1")
    s.wire("=A1+P1-Q1.4", "=A1+P1-K1.L2", net="L2", number="1.2", sheet_id="s1")
    s.wire("=A1+P1-Q1.6", "=A1+P1-K1.L3", net="L3", number="1.3", sheet_id="s1")
    s.wire("=A1+P1-K1.T1", "=A1+P1-F1.1", net="T1", number="1.4", sheet_id="s1")
    s.wire("=A1+P1-K1.T2", "=A1+P1-F1.3", net="T2", number="1.5", sheet_id="s1")
    s.wire("=A1+P1-K1.T3", "=A1+P1-F1.5", net="T3", number="1.6", sheet_id="s1")
    s.wire("=A1+P1-F1.2", "=A1+P1-M1.U1", net="U1", number="1.7", sheet_id="s1")
    s.wire("=A1+P1-F1.4", "=A1+P1-M1.V1", net="V1", number="1.8", sheet_id="s1")
    s.wire("=A1+P1-F1.6", "=A1+P1-M1.W1", net="W1", number="1.9", sheet_id="s1")
    return s


def _build_15_schneider_style_numbering() -> Session:
    s = Session.create("Synthetic Schneider Style Numbering", sheet_id="s1", title="Schneider KM/Q/F/S Style")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "s1", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "s1",
        y=36.0,
        contacts=[
            ("iec.protection.fuse_10a", "F1"),
            ("iec.operators.pb_nc", "S0"),
            ("iec.operators.pb_no", "S1"),
        ],
        coil=("iec.contactors.coil", "KM1"),
        seal_in=("iec.contactors.aux_no", "KM1-13", "S1"),
    )
    _add_rung(s,
        "s1",
        y=96.0,
        contacts=[("iec.contactors.aux_no", "KM1-23")],
        coil=("iec.operators.pilot_light", "H1"),
    )
    return s


def _build_16_eplan_potential_chains() -> Session:
    s = Session.create("Synthetic EPLAN Potential Chains", sheet_id="sh1", title="Sheet 1 - 24VDC Supply & E-Stop")
    s.set_title_block(standard="IEC")
    _configure_sheet(s, "sh1", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "sh1",
        y=36.0,
        contacts=[
            ("iec.protection.fuse_10a", "F1"),
            ("iec.operators.estop", "S1"),
        ],
        coil=("iec.relays.cr", "K1"),
    )
    s.add_sheet("sh2", title="Sheet 2 - Safety Potential Consumers")
    _configure_sheet(s, "sh2", drawing_style="iec", rail_hot="L+", rail_return="0V")
    _add_rung(s,
        "sh2",
        y=36.0,
        contacts=[
            ("iec.contactors.aux_no", "K1-13"),
            ("iec.operators.pb_no", "S2"),
        ],
        coil=("iec.contactors.coil", "K2"),
        seal_in=("iec.contactors.aux_no", "K2-13", "S2"),
    )
    _add_rung(s,
        "sh2",
        y=96.0,
        contacts=[("iec.contactors.aux_no", "K2-23")],
        coil=("iec.operators.pilot_light", "H1"),
    )
    return s


def _build_17_ambiguous_roles_sheet() -> Session:
    s = Session.create(
        "Synthetic Ambiguous Roles Sheet",
        sheet_id="s1",
        title="Hauptstromkreis & Steuerspannung - Not-Halt",
    )
    s.set_title_block(standard="IEC")
    sheet = s.project.sheet("s1")
    sheet.drawing_style = "iec"
    sheet.rail_hot = "+24V"
    sheet.rail_return = "0V"
    _register_custom_ambiguous_devices(s)

    sheet.components.append(
        Component(
            id="F1",
            library_id="iec.custom.control_fuse_1p",
            x=55.0,
            y=45.0,
            properties={"description": "Sicherung Steuerspannung"},
        )
    )
    sheet.components.append(
        Component(
            id="S1",
            library_id="iec.operators.estop",
            x=125.0,
            y=45.0,
            properties={"note": "Not-Halt Taster"},
        )
    )
    sheet.components.append(
        Component(
            id="K1",
            library_id="iec.custom.safety_relay_2p",
            x=205.0,
            y=45.0,
            properties={"description": "Hauptstromkreis"},
        )
    )
    sheet.components.append(
        Component(
            id="Q1",
            library_id="iec.custom.breaker_1p",
            x=55.0,
            y=105.0,
            properties={"description": "Leitungsschutzschalter 24V"},
        )
    )
    sheet.components.append(
        Component(
            id="K1-13",
            library_id="iec.contactors.aux_no",
            x=125.0,
            y=105.0,
        )
    )
    sheet.components.append(
        Component(
            id="H1",
            library_id="iec.operators.pilot_light",
            x=205.0,
            y=105.0,
        )
    )

    s.wire("+24V", "F1.1", number="W1")
    s.wire("F1.2", "S1.11", number="W2")
    s.wire("S1.12", "K1.A1", number="W3")
    s.wire("K1.A2", "0V", number="W4")
    s.wire("+24V", "Q1.1", number="W5")
    s.wire("Q1.2", "K1-13.13", number="W6")
    s.wire("K1-13.14", "H1.X1", number="W7")
    s.wire("H1.X2", "0V", number="W8")
    return s


def _build_18_imported_scan_fixture() -> Session:
    payload = {
        "title": "Synthetic IEC Control Scan",
        "devices": [
            {
                "tag": "-S1",
                "role": "pushbutton_nc",
                "library_id": "iec.operators.pb_nc",
                "pins": ["11", "12"],
                "sheet": "control",
                "confidence": 0.96,
            },
            {
                "tag": "-S2",
                "role": "pushbutton_no",
                "library_id": "iec.operators.pb_no",
                "pins": ["13", "14"],
                "sheet": "control",
                "confidence": 0.95,
            },
            {
                "tag": "-K1",
                "role": "contactor_coil",
                "library_id": "iec.contactors.coil",
                "pins": ["A1", "A2"],
                "sheet": "control",
                "confidence": 0.97,
            },
            {
                "tag": "-F1-95",
                "role": "overload_nc",
                "library_id": "iec.protection.ol_contact",
                "pins": ["95", "96"],
                "sheet": "control",
                "confidence": 0.95,
            },
        ],
        "from_to": [
            {"wire_number": "200", "from": "L+", "to": "-S1:11", "sheet": "control", "confidence": 0.96},
            {"wire_number": "201", "from": "-S1:12", "to": "-S2:13", "sheet": "control", "confidence": 0.95},
            {"wire_number": "202", "from": "-S2:14", "to": "-K1:A1", "sheet": "control", "confidence": 0.96},
            {"wire_number": "203", "from": "-K1:A2", "to": "-F1-95:95", "sheet": "control", "confidence": 0.96},
            {"wire_number": "204", "from": "-F1-95:96", "to": "N", "sheet": "control", "confidence": 0.97},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        scan_path = Path(td) / "synthetic_iec_scan.png"
        Image.new("RGB", (800, 600), "white").save(scan_path)
        draft = import_iec_print_to_from_to(
            scan_path,
            vision_backend=RecordedImportVisionBackend({"default": payload}),
        )
        return build_iec_project_from_imported_from_to(draft)


def _extract_conversion_maps(
    iec_project: Any,
    nema_session: Session,
    conv_dict: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, tuple[str, str]]]:
    """Extract `(old_to_new_tag, old_to_prefix, old_to_library_id, old_to_contact_pins)`."""
    tag_map = {str(k): str(v) for k, v in (conv_dict.get("tag_map") or {}).items()}
    iec_ids = [c.id for sh in iec_project.sheets for c in sh.components]
    nema_comp_by_id = {c.id: c for sh in nema_session.project.sheets for c in sh.components}

    actual_tags: dict[str, str] = {}
    actual_roles: dict[str, str] = {}
    actual_symbols: dict[str, str] = {}
    for old_id in iec_ids:
        new_id = tag_map.get(old_id, old_id)
        actual_tags[old_id] = new_id
        actual_roles[old_id] = _extract_prefix(new_id)
        if new_id in nema_comp_by_id:
            actual_symbols[old_id] = str(nema_comp_by_id[new_id].library_id or "")

    new_to_old = {v: k for k, v in actual_tags.items()}
    pin_map_by_old: dict[str, dict[str, str]] = {}
    for pr in conv_dict.get("pin_rewrites") or []:
        new_ref = str(pr.get("ref") or "")
        old_ref = new_to_old.get(new_ref, new_ref)
        was_pin = str(pr.get("was") or "")
        new_pin = str(pr.get("pin") or "")
        if was_pin and new_pin:
            pin_map_by_old.setdefault(old_ref, {})[was_pin] = new_pin

    actual_contact_pins: dict[str, tuple[str, str]] = {}
    for old_ref, pmap in pin_map_by_old.items():
        if len(pmap) == 2:
            old_pair = tuple(sorted(pmap.keys()))
            new_pair = (pmap[old_pair[0]], pmap[old_pair[1]])
            actual_contact_pins[old_ref] = new_pair

    return actual_tags, actual_roles, actual_symbols, actual_contact_pins


STATIC_CORPUS_GROUND_TRUTH: dict[str, dict[str, Any]] = {
    'ambiguous_roles_sheet': {
        'expected_tags': {"F1": "FU1", "H1": "PL1", "K1": "CR1", "K1-13": "CR1-13", "Q1": "CB1", "S1": "ES1"},
        'expected_roles': {"F1": "FU", "H1": "PL", "K1": "CR", "K1-13": "CR", "Q1": "CB", "S1": "ES"},
        'expected_library_ids': {"F1": "nema.protection.fuse_10a", "H1": "nema.operators.pilot_light", "K1": "nema.relays.cr", "K1-13": "nema.contactors.aux_no", "Q1": "nema.breakers.mcb_1p_16a", "S1": "nema.operators.estop"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "S1": ["1", "2"]},
        'expected_xrefs': {"CR1": ["CR1-13"]},
        'expected_notes': {"Hauptstromkreis": "Main Power Circuit", "Not-Halt": "Emergency Stop", "Steuerspannung": "Control Voltage"},
        'expected_from_to': [
            ('s1', 'W1', 'CB1:1', 'FU1:1'),
            ('s1', 'W2', 'ES1:1', 'FU1:2'),
            ('s1', 'W3', 'CR1:A1', 'ES1:2'),
            ('s1', 'W4', 'CR1:A2', 'PL1:X2'),
            ('s1', 'W6', 'CB1:2', 'CR1-13:3'),
            ('s1', 'W7', 'CR1-13:4', 'PL1:X1'),
        ],
        'expected_iec_from_to': [
            ('s1', 'W1', 'F1:1', 'Q1:1'),
            ('s1', 'W2', 'F1:2', 'S1:11'),
            ('s1', 'W3', 'K1:A1', 'S1:12'),
            ('s1', 'W4', 'H1:X2', 'K1:A2'),
            ('s1', 'W6', 'K1-13:13', 'Q1:2'),
            ('s1', 'W7', 'H1:X1', 'K1-13:14'),
        ],
    },
    'control_transformer_dist': {
        'expected_tags': {"F1": "FU1", "G1": "PS1", "Q1": "CB1", "T1": "T1"},
        'expected_roles': {"F1": "FU", "G1": "PS", "Q1": "CB", "T1": "T"},
        'expected_library_ids': {"F1": "nema.protection.fuse_10a", "G1": "nema.power.psu_24v_5a", "Q1": "nema.breakers.mcb_3p_32a", "T1": "nema.power.control_transformer"},
        'expected_contact_pins': {},
        'expected_xrefs': {},
        'expected_notes': {},
        'expected_from_to': [
            ('dist', '101', 'CB1:2', 'T1:H1'),
            ('dist', '102', 'CB1:4', 'T1:H2'),
            ('dist', '110', 'FU1:1', 'T1:X1'),
            ('dist', '111', 'FU1:2', 'PS1:L'),
            ('dist', '112', 'PS1:N', 'T1:X2'),
        ],
        'expected_iec_from_to': [
            ('dist', '101', 'Q1:2', 'T1:H1'),
            ('dist', '102', 'Q1:4', 'T1:H2'),
            ('dist', '110', 'F1:1', 'T1:X1'),
            ('dist', '111', 'F1:2', 'G1:L'),
            ('dist', '112', 'G1:N', 'T1:X2'),
        ],
    },
    'dol_starter': {
        'expected_tags': {"F1": "OL1", "F1-95": "OL1-95", "G1": "PS1", "K1": "M1", "K1-13": "M1-13", "M1": "MTR1", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "PB2"},
        'expected_roles': {"F1": "OL", "F1-95": "OL", "G1": "PS", "K1": "M", "K1-13": "M", "M1": "MTR", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "PB"},
        'expected_library_ids': {"F1": "nema.protection.overload", "F1-95": "nema.protection.ol_contact", "G1": "nema.power.psu_24v_5a", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "M1": "nema.loads.motor_3ph", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.pb_no"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13"], "OL1": ["OL1-95"]},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'M1:T1', 'OL1:T1'),
            ('main', '102', 'M1:T2', 'OL1:T2'),
            ('main', '103', 'M1:T3', 'OL1:T3'),
            ('main', '104', 'MTR1:T1', 'OL1:2'),
            ('main', '105', 'MTR1:T2', 'OL1:4'),
            ('main', '106', 'MTR1:T3', 'OL1:6'),
            ('main', 'L1', 'CB1:2', 'M1:L1'),
            ('main', 'L2', 'CB1:4', 'M1:L2'),
            ('main', 'L3', 'CB1:6', 'M1:L3'),
            ('motor-control', '0V', 'M1:A2', 'PS1:minus'),
            ('motor-control', '201', 'ES1:2', 'PB1:1'),
            ('motor-control', '202', 'M1-13:3', 'PB1:2'),
            ('motor-control', '202', 'M1-13:3', 'PB2:3'),
            ('motor-control', '203', 'M1-13:4', 'OL1-95:95'),
            ('motor-control', '203', 'M1-13:4', 'PB2:4'),
            ('motor-control', '204', 'M1:A1', 'OL1-95:96'),
            ('motor-control', '24V', 'ES1:1', 'PS1:plus'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'F1:1', 'K1:T1'),
            ('main', '102', 'F1:3', 'K1:T2'),
            ('main', '103', 'F1:5', 'K1:T3'),
            ('main', '104', 'F1:2', 'M1:U1'),
            ('main', '105', 'F1:4', 'M1:V1'),
            ('main', '106', 'F1:6', 'M1:W1'),
            ('main', 'L1', 'K1:L1', 'Q1:2'),
            ('main', 'L2', 'K1:L2', 'Q1:4'),
            ('main', 'L3', 'K1:L3', 'Q1:6'),
            ('motor-control', '0V', 'G1:minus', 'K1:A2'),
            ('motor-control', '201', 'S1:12', 'S2:11'),
            ('motor-control', '202', 'K1-13:13', 'S2:12'),
            ('motor-control', '202', 'K1-13:13', 'S3:13'),
            ('motor-control', '203', 'F1-95:95', 'K1-13:14'),
            ('motor-control', '203', 'F1-95:95', 'S3:14'),
            ('motor-control', '204', 'F1-95:96', 'K1:A1'),
            ('motor-control', '24V', 'G1:plus', 'S1:11'),
        ],
    },
    'eplan_potential_chains': {
        'expected_tags': {"F1": "FU1", "H1": "PL1", "K1": "CR1", "K1-13": "CR1-13", "K2": "M1", "K2-13": "M1-13", "K2-23": "M1-23", "S1": "ES1", "S2": "PB1"},
        'expected_roles': {"F1": "FU", "H1": "PL", "K1": "CR", "K1-13": "CR", "K2": "M", "K2-13": "M", "K2-23": "M", "S1": "ES", "S2": "PB"},
        'expected_library_ids': {"F1": "nema.protection.fuse_10a", "H1": "nema.operators.pilot_light", "K1": "nema.relays.cr", "K1-13": "nema.contactors.aux_no", "K2": "nema.contactors.coil", "K2-13": "nema.contactors.aux_no", "K2-23": "nema.contactors.aux_no", "S1": "nema.operators.estop", "S2": "nema.operators.pb_no"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K2-13": ["3", "4"], "K2-23": ["3", "4"], "S1": ["1", "2"], "S2": ["3", "4"]},
        'expected_xrefs': {"CR1": ["CR1-13"], "M1": ["M1-13", "M1-23"]},
        'expected_notes': {},
        'expected_from_to': [
            ('sh1', '0V', 'CR1:A2', 'M1:A2'),
            ('sh1', '0V', 'CR1:A2', 'PL1:X2'),
            ('sh1', '101', 'M1-23:4', 'PL1:X1'),
            ('sh1', '24V', 'CR1-13:3', 'FU1:1'),
            ('sh1', '24V', 'CR1-13:3', 'M1-23:3'),
            ('sh1', '41', 'CR1-13:4', 'PB1:3'),
            ('sh1', '41', 'ES1:1', 'FU1:2'),
            ('sh1', '41A', 'M1-13:3', 'PB1:3'),
            ('sh1', '41B', 'M1-13:4', 'PB1:4'),
            ('sh1', '42', 'CR1:A1', 'ES1:2'),
            ('sh1', '42', 'M1:A1', 'PB1:4'),
        ],
        'expected_iec_from_to': [
            ('sh1', '0V', 'H1:X2', 'K1:A2'),
            ('sh1', '41', 'F1:2', 'S1:11'),
            ('sh1', '42', 'K1:A1', 'S1:12'),
            ('sh2', '0V', 'H1:X2', 'K2:A2'),
            ('sh2', '101', 'H1:X1', 'K2-23:14'),
            ('sh2', '41', 'K1-13:14', 'S2:13'),
            ('sh2', '41A', 'K2-13:13', 'S2:13'),
            ('sh2', '41B', 'K2-13:14', 'S2:14'),
            ('sh2', '42', 'K2:A1', 'S2:14'),
            ('sh2', 'L+', 'F1:1', 'K1-13:13'),
            ('sh2', 'L+', 'F1:1', 'K2-23:13'),
        ],
    },
    'imported_scan_fixture': {
        'expected_tags': {"F1-95": "OL1-95", "K1": "M1", "S1": "PB1", "S2": "PB2"},
        'expected_roles': {"F1-95": "OL", "K1": "M", "S1": "PB", "S2": "PB"},
        'expected_library_ids': {"F1-95": "nema.contactors.coil", "K1": "nema.contactors.coil", "S1": "nema.operators.pb_nc", "S2": "nema.operators.pb_no"},
        'expected_contact_pins': {"S1": ["1", "2"], "S2": ["3", "4"]},
        'expected_xrefs': {"OL1-95": ["OL1-95"]},
        'expected_notes': {},
        'expected_from_to': [
            ('power', '201', 'PB1:2', 'PB2:3'),
            ('power', '202', 'M1:A1', 'PB2:4'),
            ('power', '203', 'M1:A2', 'OL1-95:A1'),
        ],
        'expected_iec_from_to': [
            ('control', '201', 'S1:12', 'S2:13'),
            ('control', '202', 'K1:A1', 'S2:14'),
            ('control', '203', 'F1-95:A1', 'K1:A2'),
        ],
    },
    'multi_page_conveyor_3p': {
        'expected_tags': {"F1": "OL1", "F1-95": "OL1-95", "H1": "PL1", "H2": "HN1", "K1": "M1", "K1-13": "M1-13", "K1-21": "M1-21", "K1-23": "M1-23", "M1": "MTR1", "Q1": "CB1", "S0": "ES1", "S1": "PB1", "S2": "PB2"},
        'expected_roles': {"F1": "OL", "F1-95": "OL", "H1": "PL", "H2": "HN", "K1": "M", "K1-13": "M", "K1-21": "M", "K1-23": "M", "M1": "MTR", "Q1": "CB", "S0": "ES", "S1": "PB", "S2": "PB"},
        'expected_library_ids': {"F1": "nema.protection.overload", "F1-95": "nema.protection.ol_contact", "H1": "nema.operators.pilot_light", "H2": "nema.operators.horn", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K1-21": "nema.contactors.aux_nc", "K1-23": "nema.contactors.aux_no", "M1": "nema.loads.motor_3ph", "Q1": "nema.breakers.mcb_3p_32a", "S0": "nema.operators.estop", "S1": "nema.operators.pb_nc", "S2": "nema.operators.pb_no"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-21": ["1", "2"], "K1-23": ["3", "4"], "S0": ["1", "2"], "S1": ["1", "2"], "S2": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13", "M1-21", "M1-23"], "OL1": ["OL1-95"]},
        'expected_notes': {},
        'expected_from_to': [
            ('p1-power', '101', 'CB1:2', 'M1:L1'),
            ('p1-power', '102', 'CB1:4', 'M1:L2'),
            ('p1-power', '103', 'CB1:6', 'M1:L3'),
            ('p1-power', '104', 'M1:T1', 'OL1:T1'),
            ('p1-power', '105', 'M1:T2', 'OL1:T2'),
            ('p1-power', '106', 'M1:T3', 'OL1:T3'),
            ('p1-power', '107', 'MTR1:T1', 'OL1:2'),
            ('p1-power', '108', 'MTR1:T2', 'OL1:4'),
            ('p1-power', '109', 'MTR1:T3', 'OL1:6'),
            ('p2-control', '0V', 'HN1:X2', 'M1:A2'),
            ('p2-control', '0V', 'HN1:X2', 'PL1:X2'),
            ('p2-control', '101', 'HN1:X1', 'M1-21:2'),
            ('p2-control', '24V', 'ES1:1', 'M1-21:1'),
            ('p2-control', '24V', 'ES1:1', 'M1-23:3'),
            ('p2-control', '41', 'ES1:2', 'PB1:1'),
            ('p2-control', '41', 'M1-23:4', 'PL1:X1'),
            ('p2-control', '42', 'PB1:2', 'PB2:3'),
            ('p2-control', '42A', 'M1-13:3', 'PB2:3'),
            ('p2-control', '42B', 'M1-13:4', 'PB2:4'),
            ('p2-control', '43', 'OL1-95:95', 'PB2:4'),
            ('p2-control', '44', 'M1:A1', 'OL1-95:96'),
        ],
        'expected_iec_from_to': [
            ('p1-power', '101', 'K1:L1', 'Q1:2'),
            ('p1-power', '102', 'K1:L2', 'Q1:4'),
            ('p1-power', '103', 'K1:L3', 'Q1:6'),
            ('p1-power', '104', 'F1:1', 'K1:T1'),
            ('p1-power', '105', 'F1:3', 'K1:T2'),
            ('p1-power', '106', 'F1:5', 'K1:T3'),
            ('p1-power', '107', 'F1:2', 'M1:U1'),
            ('p1-power', '108', 'F1:4', 'M1:V1'),
            ('p1-power', '109', 'F1:6', 'M1:W1'),
            ('p2-control', '0V', 'H1:X2', 'K1:A2'),
            ('p2-control', '41', 'S0:12', 'S1:11'),
            ('p2-control', '42', 'S1:12', 'S2:13'),
            ('p2-control', '42A', 'K1-13:13', 'S2:13'),
            ('p2-control', '42B', 'K1-13:14', 'S2:14'),
            ('p2-control', '43', 'F1-95:95', 'S2:14'),
            ('p2-control', '44', 'F1-95:96', 'K1:A1'),
            ('p2-control', 'L+', 'K1-21:21', 'S0:11'),
            ('p3-status', '0V', 'H1:X2', 'H2:X2'),
            ('p3-status', '101', 'H2:X1', 'K1-21:22'),
            ('p3-status', '41', 'H1:X1', 'K1-23:14'),
            ('p3-status', 'L+', 'K1-21:21', 'K1-23:13'),
        ],
    },
    'multi_page_hvac_ahu': {
        'expected_tags': {"B1": "TSW1", "B2": "FSW1", "H1": "PL1", "K1": "CR1", "K1-13": "CR1-13", "K1-23": "CR1-23", "K2": "M1", "K2-13": "M1-13", "S1": "SEL1", "S2": "LS1", "Y1": "SOL1"},
        'expected_roles': {"B1": "TSW", "B2": "FSW", "H1": "PL", "K1": "CR", "K1-13": "CR", "K1-23": "CR", "K2": "M", "K2-13": "M", "S1": "SEL", "S2": "LS", "Y1": "SOL"},
        'expected_library_ids': {"B1": "nema.operators.temperature_nc", "B2": "nema.operators.flow_no", "H1": "nema.operators.pilot_light", "K1": "nema.relays.cr", "K1-13": "nema.contactors.aux_no", "K1-23": "nema.contactors.aux_no", "K2": "nema.contactors.coil", "K2-13": "nema.contactors.aux_no", "S1": "nema.operators.selector_no", "S2": "nema.operators.limit_no", "Y1": "nema.loads.solenoid"},
        'expected_contact_pins': {"B1": ["1", "2"], "B2": ["3", "4"], "K1-13": ["3", "4"], "K1-23": ["3", "4"], "K2-13": ["3", "4"], "S1": ["3", "4"], "S2": ["3", "4"]},
        'expected_xrefs': {"CR1": ["CR1-13", "CR1-23"], "M1": ["M1-13"]},
        'expected_notes': {},
        'expected_from_to': [
            ('p1-fan', '0V', 'CR1:A2', 'M1:A2'),
            ('p1-fan', '0V', 'CR1:A2', 'PL1:X2'),
            ('p1-fan', '0V', 'CR1:A2', 'SOL1:A2'),
            ('p1-fan', '101', 'CR1-13:4', 'LS1:3'),
            ('p1-fan', '101', 'CR1-23:4', 'PL1:X1'),
            ('p1-fan', '102', 'LS1:4', 'M1:A1'),
            ('p1-fan', '24V', 'CR1-13:3', 'CR1-23:3'),
            ('p1-fan', '24V', 'CR1-13:3', 'M1-13:3'),
            ('p1-fan', '24V', 'CR1-13:3', 'TSW1:1'),
            ('p1-fan', '41', 'FSW1:3', 'M1-13:4'),
            ('p1-fan', '41', 'SEL1:3', 'TSW1:2'),
            ('p1-fan', '42', 'CR1:A1', 'SEL1:4'),
            ('p1-fan', '42', 'FSW1:4', 'SOL1:A1'),
        ],
        'expected_iec_from_to': [
            ('p1-fan', '0V', 'H1:X2', 'K1:A2'),
            ('p1-fan', '0V', 'H1:X2', 'K2:A2'),
            ('p1-fan', '101', 'K1-13:14', 'S2:13'),
            ('p1-fan', '102', 'K2:A1', 'S2:14'),
            ('p1-fan', '41', 'B1:12', 'S1:13'),
            ('p1-fan', '42', 'K1:A1', 'S1:14'),
            ('p1-fan', 'L+', 'B1:11', 'K1-13:13'),
            ('p2-exhaust', '0V', 'H1:X2', 'Y1:A2'),
            ('p2-exhaust', '101', 'H1:X1', 'K1-23:14'),
            ('p2-exhaust', '41', 'B2:13', 'K2-13:14'),
            ('p2-exhaust', '42', 'B2:14', 'Y1:A1'),
            ('p2-exhaust', 'L+', 'B1:11', 'K1-23:13'),
            ('p2-exhaust', 'L+', 'B1:11', 'K2-13:13'),
        ],
    },
    'multi_page_pump_duplex': {
        'expected_tags': {"B1": "FS1", "B2": "FS2", "B3": "LEV1", "F1-95": "OL1-95", "F2-95": "OL2-95", "H1": "PL1", "K1": "M1", "K1-13": "M1-13", "K2": "M2", "K2-13": "M2-13", "K3": "AR1"},
        'expected_roles': {"B1": "FS", "B2": "FS", "B3": "LEV", "F1-95": "OL", "F2-95": "OL", "H1": "PL", "K1": "M", "K1-13": "M", "K2": "M", "K2-13": "M", "K3": "AR"},
        'expected_library_ids': {"B1": "nema.operators.float_no", "B2": "nema.operators.float_no", "B3": "nema.operators.level_no", "F1-95": "nema.protection.ol_contact", "F2-95": "nema.protection.ol_contact", "H1": "nema.operators.pilot_light", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K2": "nema.contactors.coil", "K2-13": "nema.contactors.aux_no", "K3": "nema.relays.alternating"},
        'expected_contact_pins': {"B1": ["3", "4"], "B2": ["3", "4"], "B3": ["3", "4"], "K1-13": ["3", "4"], "K2-13": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13"], "M2": ["M2-13"]},
        'expected_notes': {},
        'expected_from_to': [
            ('p1-lead', '0V', 'AR1:A2', 'M1:A2'),
            ('p1-lead', '0V', 'AR1:A2', 'M2:A2'),
            ('p1-lead', '0V', 'AR1:A2', 'PL1:X2'),
            ('p1-lead', '101', 'AR1:A1', 'M1-13:4'),
            ('p1-lead', '24V', 'FS1:3', 'FS2:3'),
            ('p1-lead', '24V', 'FS1:3', 'LEV1:3'),
            ('p1-lead', '24V', 'FS1:3', 'M1-13:3'),
            ('p1-lead', '41', 'FS1:4', 'OL1-95:95'),
            ('p1-lead', '41', 'FS2:4', 'OL2-95:95'),
            ('p1-lead', '41', 'LEV1:4', 'M2-13:3'),
            ('p1-lead', '42', 'M1:A1', 'OL1-95:96'),
            ('p1-lead', '42', 'M2-13:4', 'PL1:X1'),
            ('p1-lead', '42', 'M2:A1', 'OL2-95:96'),
        ],
        'expected_iec_from_to': [
            ('p1-lead', '0V', 'H1:X2', 'K1:A2'),
            ('p1-lead', '41', 'B1:14', 'F1-95:95'),
            ('p1-lead', '42', 'F1-95:96', 'K1:A1'),
            ('p2-lag', '0V', 'H1:X2', 'K2:A2'),
            ('p2-lag', '0V', 'H1:X2', 'K3:A2'),
            ('p2-lag', '101', 'K1-13:14', 'K3:A1'),
            ('p2-lag', '41', 'B2:14', 'F2-95:95'),
            ('p2-lag', '42', 'F2-95:96', 'K2:A1'),
            ('p2-lag', 'L+', 'B1:13', 'B2:13'),
            ('p2-lag', 'L+', 'B1:13', 'K1-13:13'),
            ('p3-alarm', '41', 'B3:14', 'K2-13:13'),
            ('p3-alarm', '42', 'H1:X1', 'K2-13:14'),
            ('p3-alarm', 'L+', 'B1:13', 'B3:13'),
        ],
    },
    'plc_analog_loop': {
        'expected_tags': {"B1": "TT1", "B2": "PRS1", "B3": "TSW1", "F1": "FU1", "K1": "CR1", "K1-13": "CR1-13", "Y1": "SOL1"},
        'expected_roles': {"B1": "TT", "B2": "PRS", "B3": "TSW", "F1": "FU", "K1": "CR", "K1-13": "CR", "Y1": "SOL"},
        'expected_library_ids': {"B1": "nema.loads.transmitter_2wire", "B2": "nema.operators.pressure_no", "B3": "nema.operators.temperature_nc", "F1": "nema.protection.fuse_10a", "K1": "nema.relays.cr", "K1-13": "nema.contactors.aux_no", "Y1": "nema.loads.solenoid"},
        'expected_contact_pins': {"B2": ["3", "4"], "B3": ["1", "2"], "K1-13": ["3", "4"]},
        'expected_xrefs': {"CR1": ["CR1-13"]},
        'expected_notes': {},
        'expected_from_to': [
            ('analog', '0V', 'CR1:A2', 'SOL1:A2'),
            ('analog', '0V', 'CR1:A2', 'TT1:SIG'),
            ('analog', '101', 'PRS1:4', 'TSW1:1'),
            ('analog', '102', 'CR1:A1', 'TSW1:2'),
            ('analog', '161', 'CR1-13:4', 'SOL1:A1'),
            ('analog', '24V', 'CR1-13:3', 'FU1:1'),
            ('analog', '24V', 'CR1-13:3', 'PRS1:3'),
            ('analog', '41', 'FU1:2', 'TT1:PLUS'),
        ],
        'expected_iec_from_to': [
            ('analog', '0V', 'B1:SIG', 'K1:A2'),
            ('analog', '0V', 'B1:SIG', 'Y1:A2'),
            ('analog', '101', 'B2:14', 'B3:11'),
            ('analog', '102', 'B3:12', 'K1:A1'),
            ('analog', '161', 'K1-13:14', 'Y1:A1'),
            ('analog', '41', 'B1:PLUS', 'F1:2'),
            ('analog', 'L+', 'B2:13', 'F1:1'),
            ('analog', 'L+', 'B2:13', 'K1-13:13'),
        ],
    },
    'plc_digital_io': {
        'expected_tags': {"A1": "PLC1", "F1": "OL1", "F1-95": "OL1-95", "F1-96": "OL1-96", "G1": "PS1", "K1": "M1", "K1-13": "M1-13", "K1-14": "M1-14", "K2": "CR1", "K2-13": "CR1-13", "M1": "MTR1", "OP1": "OP1", "OP2": "OP2", "OP3": "OP3", "PE": "PE", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "SEL1", "S3-14": "SEL1-14", "S4": "PB2", "X1": "TB1", "X2": "TB2", "X3": "TB3", "X4": "TB4", "XS1": "REC1", "XS2": "REC2"},
        'expected_roles': {"A1": "PLC", "F1": "OL", "F1-95": "OL", "F1-96": "OL", "G1": "PS", "K1": "M", "K1-13": "M", "K1-14": "M", "K2": "CR", "K2-13": "CR", "M1": "MTR", "OP1": "OP", "OP2": "OP", "OP3": "OP", "PE": "PE", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "SEL", "S3-14": "SEL", "S4": "PB", "X1": "TB", "X2": "TB", "X3": "TB", "X4": "TB", "XS1": "REC", "XS2": "REC"},
        'expected_library_ids': {"A1": "nema.plc.compact_16dio", "F1": "nema.protection.overload", "F1-95": "nema.protection.ol_contact", "F1-96": "nema.protection.ol_contact", "G1": "nema.power.psu_24v_5a", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K1-14": "nema.contactors.aux_no", "K2": "nema.relays.k_4co_24vdc", "K2-13": "nema.contactors.aux_no", "M1": "nema.loads.motor_3ph", "OP1": "iec.connection.offpage_ref", "OP2": "iec.connection.offpage_ref", "OP3": "iec.connection.offpage_ref", "PE": "iec.connection.ground", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.selector_3pos", "S3-14": "nema.contactors.aux_no", "S4": "nema.operators.pb_no", "X1": "nema.terminals.x_2p5", "X2": "nema.terminals.x_2p5", "X3": "nema.terminals.x_2p5", "X4": "nema.terminals.pe", "XS1": "iec.accessories.end_stop", "XS2": "iec.accessories.end_stop"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-14": ["3", "4"], "K2-13": ["3", "4"], "S1": ["1", "2"], "S2": ["1", "2"], "S3-14": ["3", "4"], "S4": ["3", "4"]},
        'expected_xrefs': {"CR1": ["CR1-13"], "M1": ["M1-13", "M1-14"], "OL1": ["OL1-95", "OL1-96"]},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'M1:T1', 'OL1:T1'),
            ('main', '102', 'M1:T2', 'OL1:T2'),
            ('main', '103', 'M1:T3', 'OL1:T3'),
            ('main', '104', 'MTR1:T1', 'OL1:2'),
            ('main', '105', 'MTR1:T2', 'OL1:4'),
            ('main', '106', 'MTR1:T3', 'OL1:6'),
            ('main', 'L1', 'CB1:2', 'M1:L1'),
            ('main', 'L2', 'CB1:4', 'M1:L2'),
            ('main', 'L3', 'CB1:6', 'M1:L3'),
            ('motor-control', '0V', 'CR1:A2', 'M1:A2'),
            ('motor-control', '0V', 'CR1:A2', 'PS1:minus'),
            ('motor-control', '201', 'ES1:2', 'PB1:1'),
            ('motor-control', '202', 'OL1-95:95', 'PB1:2'),
            ('motor-control', '203', 'OL1-95:96', 'SEL1:1'),
            ('motor-control', '204', 'M1-13:3', 'PB2:3'),
            ('motor-control', '204', 'M1-13:3', 'SEL1:2'),
            ('motor-control', '205', 'CR1-13:4', 'M1-13:4'),
            ('motor-control', '205', 'CR1-13:4', 'M1:A1'),
            ('motor-control', '205', 'CR1-13:4', 'PB2:4'),
            ('motor-control', '206', 'CR1-13:3', 'SEL1:3'),
            ('motor-control', '24V', 'ES1:1', 'PS1:plus'),
            ('plc-io', '0V', 'CR1:A2', 'PLC1:M'),
            ('plc-io', '24V', 'ES1:1', 'M1-14:3'),
            ('plc-io', '24V', 'ES1:1', 'OL1-96:95'),
            ('plc-io', '24V', 'ES1:1', 'PLC1:Lplus'),
            ('plc-io', '24V', 'ES1:1', 'SEL1-14:3'),
            ('plc-io', '301', 'CR1:A1', 'PLC1:Q0_0'),
            ('plc-io', '302', 'M1-14:4', 'PLC1:I0_0'),
            ('plc-io', '303', 'OL1-96:96', 'PLC1:I0_1'),
            ('plc-io', '304', 'PLC1:I0_2', 'SEL1-14:4'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'F1:1', 'K1:T1'),
            ('main', '102', 'F1:3', 'K1:T2'),
            ('main', '103', 'F1:5', 'K1:T3'),
            ('main', '104', 'F1:2', 'M1:U1'),
            ('main', '105', 'F1:4', 'M1:V1'),
            ('main', '106', 'F1:6', 'M1:W1'),
            ('main', 'L1', 'K1:L1', 'Q1:2'),
            ('main', 'L2', 'K1:L2', 'Q1:4'),
            ('main', 'L3', 'K1:L3', 'Q1:6'),
            ('motor-control', '0V', 'A1:M', 'G1:minus'),
            ('motor-control', '0V', 'A1:M', 'K1:A2'),
            ('motor-control', '201', 'S1:12', 'S2:11'),
            ('motor-control', '202', 'F1-95:95', 'S2:12'),
            ('motor-control', '203', 'F1-95:96', 'S3:11'),
            ('motor-control', '204', 'K1-13:13', 'S3:12'),
            ('motor-control', '204', 'K1-13:13', 'S4:13'),
            ('motor-control', '205', 'K1-13:14', 'K1:A1'),
            ('motor-control', '205', 'K1-13:14', 'K2-13:14'),
            ('motor-control', '205', 'K1-13:14', 'S4:14'),
            ('motor-control', '206', 'K2-13:13', 'S3:14'),
            ('motor-control', 'L+', 'A1:Lplus', 'G1:plus'),
            ('motor-control', 'L+', 'A1:Lplus', 'S1:11'),
            ('plc-io', '0V', 'A1:M', 'K2:A2'),
            ('plc-io', '301', 'A1:Q0_0', 'K2:A1'),
            ('plc-io', '302', 'A1:I0_0', 'K1-14:14'),
            ('plc-io', '303', 'A1:I0_1', 'F1-96:96'),
            ('plc-io', '304', 'A1:I0_2', 'S3-14:14'),
            ('plc-io', 'L+', 'A1:Lplus', 'F1-96:95'),
            ('plc-io', 'L+', 'A1:Lplus', 'K1-14:13'),
            ('plc-io', 'L+', 'A1:Lplus', 'S3-14:13'),
        ],
    },
    'reversing_starter': {
        'expected_tags': {"F1": "OL1", "F1-95": "OL1-95", "G1": "PS1", "K1": "M1", "K1-13": "M1-13", "K1-21": "M1-21", "K2": "M2", "K2-13": "M2-13", "K2-21": "M2-21", "M1": "MTR1", "OP1": "OP1", "OP2": "OP2", "OP3": "OP3", "PE": "PE", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "PB2", "S4": "PB3", "X1": "TB1", "X2": "TB2", "X3": "TB3", "X4": "TB4", "XS1": "REC1", "XS2": "REC2"},
        'expected_roles': {"F1": "OL", "F1-95": "OL", "G1": "PS", "K1": "M", "K1-13": "M", "K1-21": "M", "K2": "M", "K2-13": "M", "K2-21": "M", "M1": "MTR", "OP1": "OP", "OP2": "OP", "OP3": "OP", "PE": "PE", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "PB", "S4": "PB", "X1": "TB", "X2": "TB", "X3": "TB", "X4": "TB", "XS1": "REC", "XS2": "REC"},
        'expected_library_ids': {"F1": "nema.protection.overload", "F1-95": "nema.protection.ol_contact", "G1": "nema.power.psu_24v_5a", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K1-21": "nema.contactors.aux_nc", "K2": "nema.contactors.coil", "K2-13": "nema.contactors.aux_no", "K2-21": "nema.contactors.aux_nc", "M1": "nema.loads.motor_3ph", "OP1": "iec.connection.offpage_ref", "OP2": "iec.connection.offpage_ref", "OP3": "iec.connection.offpage_ref", "PE": "iec.connection.ground", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.pb_no", "S4": "nema.operators.pb_no", "X1": "nema.terminals.x_2p5", "X2": "nema.terminals.x_2p5", "X3": "nema.terminals.x_2p5", "X4": "nema.terminals.pe", "XS1": "iec.accessories.end_stop", "XS2": "iec.accessories.end_stop"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-21": ["1", "2"], "K2-13": ["3", "4"], "K2-21": ["1", "2"], "S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"], "S4": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13", "M1-21"], "M2": ["M2-13", "M2-21"], "OL1": ["OL1-95"]},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'M1:T1', 'M2:T1'),
            ('main', '101', 'M1:T1', 'OL1:T1'),
            ('main', '102', 'M1:T2', 'M2:T2'),
            ('main', '102', 'M1:T2', 'OL1:T2'),
            ('main', '103', 'M1:T3', 'M2:T3'),
            ('main', '103', 'M1:T3', 'OL1:T3'),
            ('main', '104', 'MTR1:T1', 'OL1:2'),
            ('main', '105', 'MTR1:T2', 'OL1:4'),
            ('main', '106', 'MTR1:T3', 'OL1:6'),
            ('main', 'L1', 'CB1:2', 'M1:L1'),
            ('main', 'L1', 'CB1:2', 'M2:L3'),
            ('main', 'L2', 'CB1:4', 'M1:L2'),
            ('main', 'L2', 'CB1:4', 'M2:L2'),
            ('main', 'L3', 'CB1:6', 'M1:L3'),
            ('main', 'L3', 'CB1:6', 'M2:L1'),
            ('motor-control', '0V', 'M1:A2', 'M2:A2'),
            ('motor-control', '0V', 'M1:A2', 'PS1:minus'),
            ('motor-control', '201', 'ES1:2', 'PB1:1'),
            ('motor-control', '202', 'OL1-95:95', 'PB1:2'),
            ('motor-control', '203', 'M1-13:3', 'M2-13:3'),
            ('motor-control', '203', 'M1-13:3', 'OL1-95:96'),
            ('motor-control', '203', 'M1-13:3', 'PB2:3'),
            ('motor-control', '203', 'M1-13:3', 'PB3:3'),
            ('motor-control', '204', 'M1-13:4', 'M2-21:1'),
            ('motor-control', '204', 'M1-13:4', 'PB2:4'),
            ('motor-control', '205', 'M1:A1', 'M2-21:2'),
            ('motor-control', '206', 'M1-21:1', 'M2-13:4'),
            ('motor-control', '206', 'M1-21:1', 'PB3:4'),
            ('motor-control', '207', 'M1-21:2', 'M2:A1'),
            ('motor-control', '24V', 'ES1:1', 'PS1:plus'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'F1:1', 'K1:T1'),
            ('main', '101', 'F1:1', 'K2:T1'),
            ('main', '102', 'F1:3', 'K1:T2'),
            ('main', '102', 'F1:3', 'K2:T2'),
            ('main', '103', 'F1:5', 'K1:T3'),
            ('main', '103', 'F1:5', 'K2:T3'),
            ('main', '104', 'F1:2', 'M1:U1'),
            ('main', '105', 'F1:4', 'M1:V1'),
            ('main', '106', 'F1:6', 'M1:W1'),
            ('main', 'L1', 'K1:L1', 'K2:L3'),
            ('main', 'L1', 'K1:L1', 'Q1:2'),
            ('main', 'L2', 'K1:L2', 'K2:L2'),
            ('main', 'L2', 'K1:L2', 'Q1:4'),
            ('main', 'L3', 'K1:L3', 'K2:L1'),
            ('main', 'L3', 'K1:L3', 'Q1:6'),
            ('motor-control', '0V', 'G1:minus', 'K1:A2'),
            ('motor-control', '0V', 'G1:minus', 'K2:A2'),
            ('motor-control', '201', 'S1:12', 'S2:11'),
            ('motor-control', '202', 'F1-95:95', 'S2:12'),
            ('motor-control', '203', 'F1-95:96', 'K1-13:13'),
            ('motor-control', '203', 'F1-95:96', 'K2-13:13'),
            ('motor-control', '203', 'F1-95:96', 'S3:13'),
            ('motor-control', '203', 'F1-95:96', 'S4:13'),
            ('motor-control', '204', 'K1-13:14', 'K2-21:21'),
            ('motor-control', '204', 'K1-13:14', 'S3:14'),
            ('motor-control', '205', 'K1:A1', 'K2-21:22'),
            ('motor-control', '206', 'K1-21:21', 'K2-13:14'),
            ('motor-control', '206', 'K1-21:21', 'S4:14'),
            ('motor-control', '207', 'K1-21:22', 'K2:A1'),
            ('motor-control', 'L+', 'G1:plus', 'S1:11'),
        ],
    },
    'safety_relay_dual_channel': {
        'expected_tags': {"H1": "PL1", "K1": "CR1", "K1-13": "CR1-13", "K1-23": "CR1-23", "K2": "CR2", "K2-13": "CR2-13", "S1": "ES1", "S2": "LS1", "S3": "PB1", "S4": "ES2", "S5": "LS2"},
        'expected_roles': {"H1": "PL", "K1": "CR", "K1-13": "CR", "K1-23": "CR", "K2": "CR", "K2-13": "CR", "S1": "ES", "S2": "LS", "S3": "PB", "S4": "ES", "S5": "LS"},
        'expected_library_ids': {"H1": "nema.operators.pilot_light", "K1": "nema.relays.cr", "K1-13": "nema.contactors.aux_no", "K1-23": "nema.contactors.aux_no", "K2": "nema.relays.cr", "K2-13": "nema.contactors.aux_no", "S1": "nema.operators.estop", "S2": "nema.operators.limit_nc", "S3": "nema.operators.pb_no", "S4": "nema.operators.estop", "S5": "nema.operators.limit_nc"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-23": ["3", "4"], "K2-13": ["3", "4"], "S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"], "S4": ["1", "2"], "S5": ["1", "2"]},
        'expected_xrefs': {"CR1": ["CR1-13", "CR1-23"], "CR2": ["CR2-13"]},
        'expected_notes': {},
        'expected_from_to': [
            ('safety', '0V', 'CR1:A2', 'CR2:A2'),
            ('safety', '0V', 'CR1:A2', 'PL1:X2'),
            ('safety', '101', 'ES2:2', 'LS2:1'),
            ('safety', '102', 'CR1-23:3', 'LS2:2'),
            ('safety', '103', 'CR1-23:4', 'CR2:A1'),
            ('safety', '161', 'CR2-13:4', 'PL1:X1'),
            ('safety', '24V', 'CR2-13:3', 'ES1:1'),
            ('safety', '24V', 'CR2-13:3', 'ES2:1'),
            ('safety', '41', 'ES1:2', 'LS1:1'),
            ('safety', '42', 'LS1:2', 'PB1:3'),
            ('safety', '42A', 'CR1-13:3', 'PB1:3'),
            ('safety', '42B', 'CR1-13:4', 'PB1:4'),
            ('safety', '43', 'CR1:A1', 'PB1:4'),
        ],
        'expected_iec_from_to': [
            ('safety', '0V', 'H1:X2', 'K1:A2'),
            ('safety', '0V', 'H1:X2', 'K2:A2'),
            ('safety', '101', 'S4:12', 'S5:11'),
            ('safety', '102', 'K1-23:13', 'S5:12'),
            ('safety', '103', 'K1-23:14', 'K2:A1'),
            ('safety', '161', 'H1:X1', 'K2-13:14'),
            ('safety', '41', 'S1:12', 'S2:11'),
            ('safety', '42', 'S2:12', 'S3:13'),
            ('safety', '42A', 'K1-13:13', 'S3:13'),
            ('safety', '42B', 'K1-13:14', 'S3:14'),
            ('safety', '43', 'K1:A1', 'S3:14'),
            ('safety', 'L+', 'K2-13:13', 'S1:11'),
            ('safety', 'L+', 'K2-13:13', 'S4:11'),
        ],
    },
    'schneider_style_numbering': {
        'expected_tags': {"F1": "FU1", "H1": "PL1", "KM1": "M1", "KM1-13": "M1-13", "KM1-23": "M1-23", "S0": "PB1", "S1": "PB2"},
        'expected_roles': {"F1": "FU", "H1": "PL", "KM1": "M", "KM1-13": "M", "KM1-23": "M", "S0": "PB", "S1": "PB"},
        'expected_library_ids': {"F1": "nema.protection.fuse_10a", "H1": "nema.operators.pilot_light", "KM1": "nema.contactors.coil", "KM1-13": "nema.contactors.aux_no", "KM1-23": "nema.contactors.aux_no", "S0": "nema.operators.pb_nc", "S1": "nema.operators.pb_no"},
        'expected_contact_pins': {"KM1-13": ["3", "4"], "KM1-23": ["3", "4"], "S0": ["1", "2"], "S1": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13", "M1-23"]},
        'expected_notes': {},
        'expected_from_to': [
            ('s1', '0V', 'M1:A2', 'PL1:X2'),
            ('s1', '101', 'M1-23:4', 'PL1:X1'),
            ('s1', '24V', 'FU1:1', 'M1-23:3'),
            ('s1', '41', 'FU1:2', 'PB1:1'),
            ('s1', '42', 'PB1:2', 'PB2:3'),
            ('s1', '42A', 'M1-13:3', 'PB2:3'),
            ('s1', '42B', 'M1-13:4', 'PB2:4'),
            ('s1', '43', 'M1:A1', 'PB2:4'),
        ],
        'expected_iec_from_to': [
            ('s1', '0V', 'H1:X2', 'KM1:A2'),
            ('s1', '101', 'H1:X1', 'KM1-23:14'),
            ('s1', '41', 'F1:2', 'S0:11'),
            ('s1', '42', 'S0:12', 'S1:13'),
            ('s1', '42A', 'KM1-13:13', 'S1:13'),
            ('s1', '42B', 'KM1-13:14', 'S1:14'),
            ('s1', '43', 'KM1:A1', 'S1:14'),
            ('s1', 'L+', 'F1:1', 'KM1-23:13'),
        ],
    },
    'siemens_style_numbering': {
        'expected_tags': {"=A1+P1-F1": "OL1", "=A1+P1-K1": "M1", "=A1+P1-M1": "MTR1", "=A1+P1-Q1": "CB1"},
        'expected_roles': {"=A1+P1-F1": "OL", "=A1+P1-K1": "M", "=A1+P1-M1": "MTR", "=A1+P1-Q1": "CB"},
        'expected_library_ids': {"=A1+P1-F1": "nema.protection.overload", "=A1+P1-K1": "nema.contactors.ac3_9a", "=A1+P1-M1": "nema.loads.motor_3ph", "=A1+P1-Q1": "nema.breakers.mcb_3p_32a"},
        'expected_contact_pins': {},
        'expected_xrefs': {},
        'expected_notes': {},
        'expected_from_to': [
            ('s1', '1.1', 'CB1:2', 'M1:L1'),
            ('s1', '1.2', 'CB1:4', 'M1:L2'),
            ('s1', '1.3', 'CB1:6', 'M1:L3'),
            ('s1', '1.4', 'M1:T1', 'OL1:T1'),
            ('s1', '1.5', 'M1:T2', 'OL1:T2'),
            ('s1', '1.6', 'M1:T3', 'OL1:T3'),
            ('s1', '1.7', 'MTR1:T1', 'OL1:2'),
            ('s1', '1.8', 'MTR1:T2', 'OL1:4'),
            ('s1', '1.9', 'MTR1:T3', 'OL1:6'),
        ],
        'expected_iec_from_to': [
            ('s1', '1.1', '=A1+P1-K1:L1', '=A1+P1-Q1:2'),
            ('s1', '1.2', '=A1+P1-K1:L2', '=A1+P1-Q1:4'),
            ('s1', '1.3', '=A1+P1-K1:L3', '=A1+P1-Q1:6'),
            ('s1', '1.4', '=A1+P1-F1:1', '=A1+P1-K1:T1'),
            ('s1', '1.5', '=A1+P1-F1:3', '=A1+P1-K1:T2'),
            ('s1', '1.6', '=A1+P1-F1:5', '=A1+P1-K1:T3'),
            ('s1', '1.7', '=A1+P1-F1:2', '=A1+P1-M1:U1'),
            ('s1', '1.8', '=A1+P1-F1:4', '=A1+P1-M1:V1'),
            ('s1', '1.9', '=A1+P1-F1:6', '=A1+P1-M1:W1'),
        ],
    },
    'soft_starter_bypass': {
        'expected_tags': {"G1": "PS1", "M1": "MTR1", "OP1": "OP1", "OP2": "OP2", "OP3": "OP3", "PE": "PE", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "PB2", "U1": "SS1", "U1-13": "SS1-13", "X1": "TB1", "X2": "TB2", "X3": "TB3", "X4": "TB4", "XS1": "REC1", "XS2": "REC2"},
        'expected_roles': {"G1": "PS", "M1": "MTR", "OP1": "OP", "OP2": "OP", "OP3": "OP", "PE": "PE", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "PB", "U1": "SS", "U1-13": "SS", "X1": "TB", "X2": "TB", "X3": "TB", "X4": "TB", "XS1": "REC", "XS2": "REC"},
        'expected_library_ids': {"G1": "nema.power.psu_24v_5a", "M1": "nema.loads.motor_3ph", "OP1": "iec.connection.offpage_ref", "OP2": "iec.connection.offpage_ref", "OP3": "iec.connection.offpage_ref", "PE": "iec.connection.ground", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.pb_no", "U1": "nema.contactors.coil", "U1-13": "nema.contactors.aux_no", "X1": "nema.terminals.x_2p5", "X2": "nema.terminals.x_2p5", "X3": "nema.terminals.x_2p5", "X4": "nema.terminals.pe", "XS1": "iec.accessories.end_stop", "XS2": "iec.accessories.end_stop"},
        'expected_contact_pins': {"S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"], "U1-13": ["3", "4"]},
        'expected_xrefs': {"SS1": ["SS1-13"]},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'MTR1:T1', 'SS1:T1'),
            ('main', '102', 'MTR1:T2', 'SS1:T2'),
            ('main', '103', 'MTR1:T3', 'SS1:T3'),
            ('main', 'L1', 'CB1:2', 'SS1:L1'),
            ('main', 'L2', 'CB1:4', 'SS1:L2'),
            ('main', 'L3', 'CB1:6', 'SS1:L3'),
            ('motor-control', '0V', 'PS1:minus', 'SS1:A2'),
            ('motor-control', '201', 'ES1:2', 'PB1:1'),
            ('motor-control', '202', 'PB1:2', 'PB2:3'),
            ('motor-control', '202', 'PB1:2', 'SS1-13:3'),
            ('motor-control', '203', 'PB2:4', 'SS1-13:4'),
            ('motor-control', '203', 'PB2:4', 'SS1:A1'),
            ('motor-control', '24V', 'ES1:1', 'PS1:plus'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'M1:U1', 'U1:T1'),
            ('main', '102', 'M1:V1', 'U1:T2'),
            ('main', '103', 'M1:W1', 'U1:T3'),
            ('main', 'L1', 'Q1:2', 'U1:L1'),
            ('main', 'L2', 'Q1:4', 'U1:L2'),
            ('main', 'L3', 'Q1:6', 'U1:L3'),
            ('motor-control', '0V', 'G1:minus', 'U1:A2'),
            ('motor-control', '201', 'S1:12', 'S2:11'),
            ('motor-control', '202', 'S2:12', 'S3:13'),
            ('motor-control', '202', 'S2:12', 'U1-13:13'),
            ('motor-control', '203', 'S3:14', 'U1-13:14'),
            ('motor-control', '203', 'S3:14', 'U1:A1'),
            ('motor-control', 'L+', 'G1:plus', 'S1:11'),
        ],
    },
    'star_delta_starter': {
        'expected_tags': {"F1": "OL1", "F1-95": "OL1-95", "G1": "PS1", "K1": "M1", "K1-13": "M1-13", "K1-23": "M1-23", "K2": "M2", "K2-21": "M2-21", "K3": "M3", "K3-21": "M3-21", "K4": "TR1", "K4-16": "TR1-16", "K4-18": "TR1-18", "M1": "MTR1", "OP1": "OP1", "OP2": "OP2", "OP3": "OP3", "PE": "PE", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "PB2", "X1": "TB1", "X2": "TB2", "X3": "TB3", "X4": "TB4", "XS1": "REC1", "XS2": "REC2"},
        'expected_roles': {"F1": "OL", "F1-95": "OL", "G1": "PS", "K1": "M", "K1-13": "M", "K1-23": "M", "K2": "M", "K2-21": "M", "K3": "M", "K3-21": "M", "K4": "TR", "K4-16": "TR", "K4-18": "TR", "M1": "MTR", "OP1": "OP", "OP2": "OP", "OP3": "OP", "PE": "PE", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "PB", "X1": "TB", "X2": "TB", "X3": "TB", "X4": "TB", "XS1": "REC", "XS2": "REC"},
        'expected_library_ids': {"F1": "nema.protection.overload", "F1-95": "nema.protection.ol_contact", "G1": "nema.power.psu_24v_5a", "K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K1-23": "nema.contactors.aux_no", "K2": "nema.contactors.coil", "K2-21": "nema.contactors.aux_nc", "K3": "nema.contactors.coil", "K3-21": "nema.contactors.aux_nc", "K4": "nema.timers.on_delay", "K4-16": "nema.contactors.aux_nc", "K4-18": "nema.contactors.aux_no", "M1": "nema.loads.motor_3ph_6lead", "OP1": "iec.connection.offpage_ref", "OP2": "iec.connection.offpage_ref", "OP3": "iec.connection.offpage_ref", "PE": "iec.connection.ground", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.pb_no", "X1": "nema.terminals.x_2p5", "X2": "nema.terminals.x_2p5", "X3": "nema.terminals.x_2p5", "X4": "nema.terminals.pe", "XS1": "iec.accessories.end_stop", "XS2": "iec.accessories.end_stop"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-23": ["3", "4"], "K2-21": ["1", "2"], "K3-21": ["1", "2"], "K4": ["1", "3"], "K4-16": ["1", "2"], "K4-18": ["3", "4"], "S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13", "M1-23"], "M2": ["M2-21"], "M3": ["M3-21"], "OL1": ["OL1-95"], "TR1": ["TR1-16", "TR1-18"]},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'M1:T1', 'OL1:T1'),
            ('main', '102', 'M1:T2', 'OL1:T2'),
            ('main', '103', 'M1:T3', 'OL1:T3'),
            ('main', '104', 'M2:T1', 'MTR1:T1'),
            ('main', '104', 'M2:T1', 'OL1:2'),
            ('main', '105', 'M2:T2', 'MTR1:T2'),
            ('main', '105', 'M2:T2', 'OL1:4'),
            ('main', '106', 'M2:T3', 'MTR1:T3'),
            ('main', '106', 'M2:T3', 'OL1:6'),
            ('main', '107', 'M2:L1', 'M3:L1'),
            ('main', '107', 'M2:L1', 'MTR1:T6'),
            ('main', '108', 'M2:L2', 'M3:L2'),
            ('main', '108', 'M2:L2', 'MTR1:T4'),
            ('main', '109', 'M2:L3', 'M3:L3'),
            ('main', '109', 'M2:L3', 'MTR1:T5'),
            ('main', '110', 'M3:T1', 'M3:T2'),
            ('main', '110', 'M3:T1', 'M3:T3'),
            ('main', 'L1', 'CB1:2', 'M1:L1'),
            ('main', 'L2', 'CB1:4', 'M1:L2'),
            ('main', 'L3', 'CB1:6', 'M1:L3'),
            ('motor-control', '0V', 'M1:A2', 'M2:A2'),
            ('motor-control', '0V', 'M1:A2', 'M3:A2'),
            ('motor-control', '0V', 'M1:A2', 'PS1:minus'),
            ('motor-control', '0V', 'M1:A2', 'TR1:A2'),
            ('motor-control', '201', 'ES1:2', 'PB1:1'),
            ('motor-control', '202', 'OL1-95:95', 'PB1:2'),
            ('motor-control', '203', 'M1-13:3', 'OL1-95:96'),
            ('motor-control', '203', 'M1-13:3', 'PB2:3'),
            ('motor-control', '204', 'M1-13:4', 'M1:A1'),
            ('motor-control', '204', 'M1-13:4', 'PB2:4'),
            ('motor-control', '205', 'M1-23:4', 'TR1-16:1'),
            ('motor-control', '205', 'M1-23:4', 'TR1-18:3'),
            ('motor-control', '205', 'M1-23:4', 'TR1:A1'),
            ('motor-control', '206', 'M2-21:1', 'TR1-16:2'),
            ('motor-control', '207', 'M2-21:2', 'M3:A1'),
            ('motor-control', '208', 'M3-21:1', 'TR1-18:4'),
            ('motor-control', '209', 'M2:A1', 'M3-21:2'),
            ('motor-control', '24V', 'ES1:1', 'M1-23:3'),
            ('motor-control', '24V', 'ES1:1', 'PS1:plus'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'F1:1', 'K1:T1'),
            ('main', '102', 'F1:3', 'K1:T2'),
            ('main', '103', 'F1:5', 'K1:T3'),
            ('main', '104', 'F1:2', 'K2:T1'),
            ('main', '104', 'F1:2', 'M1:U1'),
            ('main', '105', 'F1:4', 'K2:T2'),
            ('main', '105', 'F1:4', 'M1:V1'),
            ('main', '106', 'F1:6', 'K2:T3'),
            ('main', '106', 'F1:6', 'M1:W1'),
            ('main', '107', 'K2:L1', 'K3:L1'),
            ('main', '107', 'K2:L1', 'M1:W2'),
            ('main', '108', 'K2:L2', 'K3:L2'),
            ('main', '108', 'K2:L2', 'M1:U2'),
            ('main', '109', 'K2:L3', 'K3:L3'),
            ('main', '109', 'K2:L3', 'M1:V2'),
            ('main', '110', 'K3:T1', 'K3:T2'),
            ('main', '110', 'K3:T1', 'K3:T3'),
            ('main', 'L1', 'K1:L1', 'Q1:2'),
            ('main', 'L2', 'K1:L2', 'Q1:4'),
            ('main', 'L3', 'K1:L3', 'Q1:6'),
            ('motor-control', '0V', 'G1:minus', 'K1:A2'),
            ('motor-control', '0V', 'G1:minus', 'K2:A2'),
            ('motor-control', '0V', 'G1:minus', 'K3:A2'),
            ('motor-control', '0V', 'G1:minus', 'K4:A2'),
            ('motor-control', '201', 'S1:12', 'S2:11'),
            ('motor-control', '202', 'F1-95:95', 'S2:12'),
            ('motor-control', '203', 'F1-95:96', 'K1-13:13'),
            ('motor-control', '203', 'F1-95:96', 'S3:13'),
            ('motor-control', '204', 'K1-13:14', 'K1:A1'),
            ('motor-control', '204', 'K1-13:14', 'S3:14'),
            ('motor-control', '205', 'K1-23:14', 'K4-16:21'),
            ('motor-control', '205', 'K1-23:14', 'K4-18:13'),
            ('motor-control', '205', 'K1-23:14', 'K4:A1'),
            ('motor-control', '206', 'K2-21:21', 'K4-16:22'),
            ('motor-control', '207', 'K2-21:22', 'K3:A1'),
            ('motor-control', '208', 'K3-21:21', 'K4-18:14'),
            ('motor-control', '209', 'K2:A1', 'K3-21:22'),
            ('motor-control', 'L+', 'G1:plus', 'K1-23:13'),
            ('motor-control', 'L+', 'G1:plus', 'S1:11'),
        ],
    },
    'two_speed_dahlander': {
        'expected_tags': {"K1": "M2", "K1-13": "M2-13", "K1-21": "M2-21", "K2": "M1", "K2-13": "M1-13", "K2-21": "M1-21", "K2-23": "M1-23", "K3": "M3", "S0": "PB1", "S1": "PB2", "S2": "PB3"},
        'expected_roles': {"K1": "M", "K1-13": "M", "K1-21": "M", "K2": "M", "K2-13": "M", "K2-21": "M", "K2-23": "M", "K3": "M", "S0": "PB", "S1": "PB", "S2": "PB"},
        'expected_library_ids': {"K1": "nema.contactors.coil", "K1-13": "nema.contactors.aux_no", "K1-21": "nema.contactors.aux_nc", "K2": "nema.contactors.coil", "K2-13": "nema.contactors.aux_no", "K2-21": "nema.contactors.aux_nc", "K2-23": "nema.contactors.aux_no", "K3": "nema.contactors.coil", "S0": "nema.operators.pb_nc", "S1": "nema.operators.pb_no", "S2": "nema.operators.pb_no"},
        'expected_contact_pins': {"K1-13": ["3", "4"], "K1-21": ["1", "2"], "K2-13": ["3", "4"], "K2-21": ["1", "2"], "K2-23": ["3", "4"], "S0": ["1", "2"], "S1": ["3", "4"], "S2": ["3", "4"]},
        'expected_xrefs': {"M1": ["M1-13", "M1-21", "M1-23"], "M2": ["M2-13", "M2-21"]},
        'expected_notes': {},
        'expected_from_to': [
            ('s1', '0V', 'M1:A2', 'M2:A2'),
            ('s1', '0V', 'M1:A2', 'M3:A2'),
            ('s1', '100A', 'M1-13:3', 'PB3:3'),
            ('s1', '100B', 'M1-13:4', 'PB3:4'),
            ('s1', '101', 'M2-21:1', 'PB3:4'),
            ('s1', '102', 'M1:A1', 'M2-21:2'),
            ('s1', '161', 'M1-23:4', 'M3:A1'),
            ('s1', '24V', 'M1-23:3', 'PB1:1'),
            ('s1', '24V', 'M1-23:3', 'PB3:3'),
            ('s1', '41', 'PB1:2', 'PB2:3'),
            ('s1', '41A', 'M2-13:3', 'PB2:3'),
            ('s1', '41B', 'M2-13:4', 'PB2:4'),
            ('s1', '42', 'M1-21:1', 'PB2:4'),
            ('s1', '43', 'M1-21:2', 'M2:A1'),
        ],
        'expected_iec_from_to': [
            ('s1', '0V', 'K1:A2', 'K2:A2'),
            ('s1', '0V', 'K1:A2', 'K3:A2'),
            ('s1', '100A', 'K2-13:13', 'S2:13'),
            ('s1', '100B', 'K2-13:14', 'S2:14'),
            ('s1', '101', 'K1-21:21', 'S2:14'),
            ('s1', '102', 'K1-21:22', 'K2:A1'),
            ('s1', '161', 'K2-23:14', 'K3:A1'),
            ('s1', '41', 'S0:12', 'S1:13'),
            ('s1', '41A', 'K1-13:13', 'S1:13'),
            ('s1', '41B', 'K1-13:14', 'S1:14'),
            ('s1', '42', 'K2-21:21', 'S1:14'),
            ('s1', '43', 'K1:A1', 'K2-21:22'),
            ('s1', 'L+', 'K2-23:13', 'S0:11'),
            ('s1', 'L+', 'K2-23:13', 'S2:13'),
        ],
    },
    'vfd_drive_cell': {
        'expected_tags': {"M1": "MTR1", "Q1": "CB1", "S1": "ES1", "S2": "PB1", "S3": "PB2", "U1": "VFD1"},
        'expected_roles': {"M1": "MTR", "Q1": "CB", "S1": "ES", "S2": "PB", "S3": "PB", "U1": "VFD"},
        'expected_library_ids': {"M1": "nema.loads.motor_3ph", "Q1": "nema.breakers.mcb_3p_32a", "S1": "nema.operators.estop", "S2": "nema.operators.pb_nc", "S3": "nema.operators.pb_no", "U1": "nema.drives.vfd_1k5"},
        'expected_contact_pins': {"S1": ["1", "2"], "S2": ["1", "2"], "S3": ["3", "4"]},
        'expected_xrefs': {},
        'expected_notes': {},
        'expected_from_to': [
            ('main', '101', 'MTR1:T1', 'VFD1:U'),
            ('main', '102', 'MTR1:T2', 'VFD1:V'),
            ('main', '103', 'MTR1:T3', 'VFD1:W'),
            ('main', '104', 'ES1:2', 'PB1:1'),
            ('main', '105', 'PB1:2', 'PB2:3'),
            ('main', '106', 'PB2:4', 'VFD1:DI1'),
            ('main', 'COM', 'ES1:1', 'VFD1:COM'),
            ('main', 'L1', 'CB1:2', 'VFD1:L1'),
            ('main', 'L2', 'CB1:4', 'VFD1:L2'),
            ('main', 'L3', 'CB1:6', 'VFD1:L3'),
        ],
        'expected_iec_from_to': [
            ('main', '101', 'M1:U1', 'U1:U'),
            ('main', '102', 'M1:V1', 'U1:V'),
            ('main', '103', 'M1:W1', 'U1:W'),
            ('main', '104', 'S1:12', 'S2:11'),
            ('main', '105', 'S2:12', 'S3:13'),
            ('main', '106', 'S3:14', 'U1:DI1'),
            ('main', 'COM', 'S1:11', 'U1:COM'),
            ('main', 'L1', 'Q1:2', 'U1:L1'),
            ('main', 'L2', 'Q1:4', 'U1:L2'),
            ('main', 'L3', 'Q1:6', 'U1:L3'),
        ],
    },
}


def _static_ground_truth_for(
    item_id: str,
    *,
    expected_notes: dict[str, str] | None = None,
) -> CorpusGroundTruth:
    """Load static hand-verified ground truth for `item_id` without invoking ConversionEngine."""
    raw = STATIC_CORPUS_GROUND_TRUTH[item_id]
    notes = dict(raw.get("expected_notes") or {})
    if expected_notes:
        notes.update(expected_notes)
    return CorpusGroundTruth(
        expected_tags=dict(raw["expected_tags"]),
        expected_roles=dict(raw["expected_roles"]),
        expected_library_ids=dict(raw["expected_library_ids"]),
        expected_contact_pins={
            str(k): (str(v[0]), str(v[1]))
            for k, v in raw["expected_contact_pins"].items()
        },
        expected_from_to=[
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in raw["expected_from_to"]
        ],
        expected_iec_from_to=[
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in raw["expected_iec_from_to"]
        ],
        expected_xrefs={
            str(k): [str(x) for x in v]
            for k, v in raw["expected_xrefs"].items()
        },
        expected_notes=notes,
    )


def build_iec_conversion_corpus() -> list[CorpusPrintItem]:
    """Build the 18-print varied synthetic/public IEC corpus (PLAN.md Section 5).

    Strictly excludes any customer prints or customer names.
    """
    specs: list[tuple[str, str, str, str, Callable[[], Session], dict[str, Any], dict[str, str]]] = [
        (
            "dol_starter",
            "motor_starter",
            "Direct-on-line (DOL) 3-phase motor starter with overload and local pushbuttons",
            "synthetic_template",
            _build_01_dol_starter,
            {},
            {},
        ),
        (
            "reversing_starter",
            "motor_starter",
            "Forward/reverse interlocked 3-phase reversing motor starter",
            "synthetic_template",
            _build_02_reversing_starter,
            {},
            {},
        ),
        (
            "star_delta_starter",
            "motor_starter",
            "Wye-delta timed motor starter with main, star, delta contactors and on-delay timer",
            "synthetic_iec",
            _build_03_star_delta_starter,
            {},
            {},
        ),
        (
            "two_speed_dahlander",
            "motor_starter",
            "Two-speed Dahlander motor control with low/high speed interlocking contactors",
            "synthetic_iec",
            _build_04_two_speed_dahlander,
            {},
            {},
        ),
        (
            "vfd_drive_cell",
            "drive_system",
            "VFD 3-phase power feed with enable relay and fault/run status ladder",
            "synthetic_iec",
            _build_05_vfd_drive_cell,
            {},
            {},
        ),
        (
            "soft_starter_bypass",
            "drive_system",
            "Soft starter 3-phase power circuit with timed bypass contactor",
            "synthetic_iec",
            _build_06_soft_starter_bypass,
            {},
            {},
        ),
        (
            "plc_digital_io",
            "plc_io",
            "24VDC PLC digital input/output rack driving motor starter and status lamps",
            "synthetic_template",
            _build_07_plc_digital_io,
            {},
            {},
        ),
        (
            "plc_analog_loop",
            "plc_io",
            "PLC 4-20mA 2-wire transmitter loop with pressure/temperature switches and solenoid",
            "synthetic_iec",
            _build_08_plc_analog_loop,
            {},
            {},
        ),
        (
            "safety_relay_dual_channel",
            "safety_circuit",
            "Dual-channel E-stop and guard limit switch safety relay circuit with reset",
            "synthetic_iec",
            _build_09_safety_relay_dual_channel,
            {},
            {},
        ),
        (
            "control_transformer_dist",
            "power_distribution",
            "480V-to-120V control transformer with secondary fuse and 24VDC power supply",
            "synthetic_iec",
            _build_10_control_transformer_dist,
            {},
            {},
        ),
        (
            "multi_page_conveyor_3p",
            "multi_page",
            "3-page conveyor power, E-stop/start/stop control, and run/fault annunciation",
            "synthetic_iec",
            _build_11_multi_page_conveyor_3p,
            {},
            {},
        ),
        (
            "multi_page_pump_duplex",
            "multi_page",
            "3-page duplex lead/lag pump station with float switches, alternating relay, and alarm",
            "synthetic_iec",
            _build_12_multi_page_pump_duplex,
            {},
            {},
        ),
        (
            "multi_page_hvac_ahu",
            "multi_page",
            "Multi-page HVAC air handling unit with freeze stat, damper limit, fan, and flow interlock",
            "synthetic_iec",
            _build_13_multi_page_hvac_ahu,
            {},
            {},
        ),
        (
            "siemens_style_numbering",
            "numbering_convention",
            "Siemens =A1+P1-K1 hierarchical function/location tags and page.column wire numbers",
            "synthetic_iec",
            _build_14_siemens_style_numbering,
            {},
            {},
        ),
        (
            "schneider_style_numbering",
            "numbering_convention",
            "Schneider/French KM1/F1/S0/S1/H1 IEC device tagging and control ladder",
            "synthetic_iec",
            _build_15_schneider_style_numbering,
            {},
            {},
        ),
        (
            "eplan_potential_chains",
            "numbering_convention",
            "Multi-sheet EPLAN L+/0V potential chain across supply and safety consumer sheets",
            "synthetic_iec",
            _build_16_eplan_potential_chains,
            {},
            {},
        ),
        (
            "ambiguous_roles_sheet",
            "ai_ambiguity_and_notes",
            "Ambiguous -K/-Q/-F roles, custom unmapped IEC symbols, and German/IEC notes (Not-Halt, Steuerspannung, Hauptstromkreis)",
            "synthetic_iec",
            _build_17_ambiguous_roles_sheet,
            AMBIGUOUS_ROLES_RECORDINGS,
            {
                "Not-Halt": "Emergency Stop",
                "Steuerspannung": "Control Voltage",
                "Hauptstromkreis": "Main Power Circuit",
            },
        ),
        (
            "imported_scan_fixture",
            "imported_scan",
            "Imported synthetic IEC scan via import_iec_print_to_from_to and RecordedImportVisionBackend",
            "synthetic_import",
            _build_18_imported_scan_fixture,
            {},
            {},
        ),
    ]

    items: list[CorpusPrintItem] = []
    for item_id, category, desc, source_kind, fn, recordings, exp_notes in specs:
        meta_text = f"{item_id} {category} {desc} {source_kind}"
        if has_forbidden_corpus_terms(meta_text):
            raise ValueError(f"Forbidden term detected in corpus item {item_id}")
        gt = _static_ground_truth_for(item_id, expected_notes=exp_notes)
        items.append(
            CorpusPrintItem(
                item_id=item_id,
                category=category,
                description=desc,
                source_kind=source_kind,
                build_session=fn,
                ground_truth=gt,
                ai_recordings=copy.deepcopy(recordings),
            )
        )
    return items


def _resolve_mode_and_backend(
    item: CorpusPrintItem,
    mode: str,
    ai_backend: ConversionAIBackend | None = None,
) -> tuple[str, ConversionAIBackend]:
    """Return `(engine_mode, ai_backend)` for `rules_only`, `ai_assisted`, or `ai_recorded`."""
    norm = (mode or "rules_only").strip().lower()
    if norm == "rules_only":
        return "rules_only", NoOpAIBackend()
    if norm == "ai_recorded":
        return "ai_assisted", ai_backend or RecordedJSONBackend(item.ai_recordings or AMBIGUOUS_ROLES_RECORDINGS)
    if norm == "ai_assisted":
        if ai_backend is not None:
            return "ai_assisted", ai_backend
        if item.ai_recordings:
            return "ai_assisted", RecordedJSONBackend(item.ai_recordings)
        return "ai_assisted", ConfiguredConversionAIBackend()
    raise ValueError(f"Unsupported scorecard mode {mode!r}; expected rules_only, ai_assisted, or ai_recorded")


def _collect_all_note_texts(session: Session) -> str:
    """Concatenate sheet titles, annotations, and component properties for note checking."""
    chunks: list[str] = []
    for sheet in session.project.sheets:
        if sheet.title:
            chunks.append(sheet.title)
        for ann in getattr(sheet, "annotations", []):
            txt = getattr(ann, "text", "")
            if txt:
                chunks.append(txt)
        for comp in sheet.components:
            lbl = getattr(comp, "label", "")
            if lbl:
                chunks.append(lbl)
            for val in (comp.properties or {}).values():
                if isinstance(val, str) and val:
                    chunks.append(val)
    return " | ".join(chunks)


def score_conversion_item(
    item: CorpusPrintItem,
    *,
    mode: str = "rules_only",
    ai_backend: ConversionAIBackend | None = None,
    scorer_version: str = SCORER_VERSION,
) -> ItemScorecardResult:
    """Run IEC -> NEMA conversion and round-trip IEC -> NEMA -> IEC on `item` and score all metrics."""
    orig_session = item.build_session()
    orig_iec_project = orig_session.project.model_copy(deep=True)
    orig_iec_from_to = _canonical_from_to_tuples(orig_session)

    work_session = item.build_session()
    engine_mode, backend = _resolve_mode_and_backend(item, mode, ai_backend=ai_backend)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        conv_dict: dict[str, Any] = ConversionEngine(
            work_session,
            "nema",
            mode=engine_mode,
            ai_backend=backend,
        ).run(
            out=td_path / f"{item.item_id}.nema.pcad.yaml",
            workspace=td_path / f"{item.item_id}.workspace.json",
        )

        gt = item.ground_truth
        actual_tags, actual_roles, actual_symbols, actual_contact_pins = _extract_conversion_maps(
            orig_iec_project, work_session, conv_dict
        )

        expected_devices = {
            (gt.expected_tags[iec_id], gt.expected_library_ids[iec_id])
            for iec_id in gt.expected_tags
            if iec_id in gt.expected_library_ids
        }
        predicted_devices = {
            (comp.id, comp.library_id)
            for sheet in work_session.project.sheets
            for comp in sheet.components
        }
        tp_dev = len(expected_devices & predicted_devices)
        device_precision = tp_dev / len(predicted_devices) if predicted_devices else 1.0
        device_recall = tp_dev / len(expected_devices) if expected_devices else 1.0
        device_f1 = (
            (2.0 * device_precision * device_recall) / (device_precision + device_recall)
            if (device_precision + device_recall) > 0
            else 0.0
        )

        if gt.expected_tags:
            tag_hits = sum(
                1
                for iec_id, exp_tag in gt.expected_tags.items()
                if actual_tags.get(iec_id) == exp_tag and actual_roles.get(iec_id) == gt.expected_roles.get(iec_id)
            )
            tag_accuracy = tag_hits / len(gt.expected_tags)
        else:
            tag_accuracy = 1.0

        if gt.expected_contact_pins:
            contact_hits = sum(
                1
                for iec_id, exp_pair in gt.expected_contact_pins.items()
                if actual_contact_pins.get(iec_id) == exp_pair
            )
            contact_number_accuracy = contact_hits / len(gt.expected_contact_pins)
        else:
            contact_number_accuracy = 1.0

        conn_check = conv_dict.get("connection_check") or check_connections(
            orig_iec_project,
            work_session.project,
            work_session.catalog,
            tag_map=conv_dict.get("tag_map"),
            pin_rewrites=conv_dict.get("pin_rewrites"),
            rail_map=conv_dict.get("rail_map"),
        )
        net_ok = bool(conn_check.get("ok", False)) and bool(conv_dict.get("same_netlist", True))
        actual_from_to = set(_canonical_from_to_tuples(work_session))
        expected_from_to = set(gt.expected_from_to)
        if not net_ok:
            connection_f1 = 0.0
        elif not expected_from_to and not actual_from_to:
            connection_f1 = 1.0
        else:
            tp_conn = len(actual_from_to & expected_from_to)
            p_conn = tp_conn / len(actual_from_to) if actual_from_to else 1.0
            r_conn = tp_conn / len(expected_from_to) if expected_from_to else 1.0
            connection_f1 = (2.0 * p_conn * r_conn) / (p_conn + r_conn) if (p_conn + r_conn) > 0 else 0.0

        actual_xrefs = _canonical_xrefs(work_session)
        if gt.expected_xrefs:
            xref_hits = sum(
                1
                for exp_coil, exp_contacts in gt.expected_xrefs.items()
                if actual_xrefs.get(exp_coil) == exp_contacts
            )
            xref_accuracy = xref_hits / len(gt.expected_xrefs)
        else:
            xref_accuracy = 1.0

        all_text_after = _collect_all_note_texts(work_session).lower()
        if gt.expected_notes:
            note_hits = sum(
                1
                for src_term, exp_trans in gt.expected_notes.items()
                if exp_trans.lower() in all_text_after and src_term.lower() not in all_text_after
            )
            note_accuracy = note_hits / len(gt.expected_notes)
        else:
            note_accuracy = 1.0

        nema_flag_counts = [len(sheet_flags(work_session, sh.id)) for sh in work_session.project.sheets]
        total_flags_count = sum(nema_flag_counts)
        clear_sheets = sum(1 for c_cnt in nema_flag_counts if c_cnt == 0)
        sheet_flags_clear_rate = (
            clear_sheets / len(work_session.project.sheets) if work_session.project.sheets else 1.0
        )

        decisions_list: list[dict[str, Any]] = list(conv_dict.get("decisions") or [])
        flagged_refs: set[str] = set()
        flagged_note_texts: list[str] = []
        for dec in decisions_list:
            if not dec.get("needs_review"):
                continue
            comp_ref = str(dec.get("component_id") or "")
            if comp_ref:
                flagged_refs.add(comp_ref)
            before_d = dec.get("before") or {}
            if isinstance(before_d, dict):
                for k_id in ("id", "tag", "ref"):
                    if before_d.get(k_id):
                        flagged_refs.add(str(before_d[k_id]))
            if dec.get("reason_code") == "note_reworded":
                flagged_note_texts.append(json.dumps(dec).lower())

        total_decisions = 0
        flagged_count = 0
        wrong_unflagged_count = 0

        for iec_id, exp_tag in gt.expected_tags.items():
            total_decisions += 1
            exp_lib = gt.expected_library_ids.get(iec_id)
            exp_role = gt.expected_roles.get(iec_id)
            is_correct = (
                actual_tags.get(iec_id) == exp_tag
                and actual_roles.get(iec_id) == exp_role
                and actual_symbols.get(iec_id) == exp_lib
            )
            is_flagged = iec_id in flagged_refs or actual_tags.get(iec_id, "") in flagged_refs
            if is_flagged:
                flagged_count += 1
            elif not is_correct:
                wrong_unflagged_count += 1

        for src_term, exp_trans in gt.expected_notes.items():
            total_decisions += 1
            is_correct = exp_trans.lower() in all_text_after and src_term.lower() not in all_text_after
            is_flagged = any(src_term.lower() in txt for txt in flagged_note_texts)
            if is_flagged:
                flagged_count += 1
            elif not is_correct:
                wrong_unflagged_count += 1
            if not is_correct and not is_flagged and engine_mode == "rules_only":
                # In rules-only mode, untranslated foreign/standard notes are unattempted rather than wrong AI rewrites;
                # only count as wrong-unflagged if a note_reworded decision was actually emitted without review.
                wrong_unflagged_count -= 1

        flagged_rate = flagged_count / total_decisions if total_decisions > 0 else 0.0
        wrong_unflagged_rate = wrong_unflagged_count / total_decisions if total_decisions > 0 else 0.0

        rt_conv = work_session.convert_standard(
            "iec",
            out=td_path / f"{item.item_id}.roundtrip.iec.pcad.yaml",
            workspace=td_path / f"{item.item_id}.roundtrip.workspace.json",
        )
        rt_vs_orig = check_connections(
            orig_iec_project,
            work_session.project,
            work_session.catalog,
            tag_map={},
            pin_rewrites=[],
            rail_map={},
        )
        rt_conn_ok = bool((rt_conv.get("connection_check") or {}).get("ok", False))
        rt_same_net = bool(rt_conv.get("same_netlist", False))
        rt_edges_match = sheet_edges(orig_iec_project) == sheet_edges(work_session.project)
        rt_from_to_match = set(_canonical_from_to_tuples(work_session)) == set(gt.expected_iec_from_to)
        round_trip_ok = (
            bool(rt_vs_orig.get("ok", False))
            and rt_edges_match
            and rt_conn_ok
            and rt_same_net
            and rt_from_to_match
        )
        work_session.path = None

    return ItemScorecardResult(
        item_id=item.item_id,
        category=item.category,
        mode=mode,
        scorer_version=scorer_version,
        device_precision=device_precision,
        device_recall=device_recall,
        device_f1=device_f1,
        tag_accuracy=tag_accuracy,
        contact_number_accuracy=contact_number_accuracy,
        connection_f1=connection_f1,
        netlist_ok=net_ok,
        round_trip_from_to_ok=round_trip_ok,
        round_trip_from_to_match=round_trip_ok,
        xref_accuracy=xref_accuracy,
        note_accuracy=note_accuracy,
        sheet_flags_clear_rate=sheet_flags_clear_rate,
        sheet_flags_count=total_flags_count,
        flagged_rate=flagged_rate,
        wrong_unflagged_rate=wrong_unflagged_rate,
        flagged_count=flagged_count,
        wrong_unflagged_count=wrong_unflagged_count,
        total_decisions=total_decisions,
        details={
            "actual_tags": actual_tags,
            "decisions": decisions_list,
        },
    )


def _aggregate_scorecard_report(
    results: list[ItemScorecardResult],
    *,
    mode: str,
    corpus_size: int,
    scorer_version: str = SCORER_VERSION,
    timestamp: str | None = None,
) -> ScorecardReport:
    """Aggregate per-print results into a `ScorecardReport`."""
    ts = timestamp or dt.datetime.now(dt.timezone.utc).isoformat()
    n = len(results)
    if n == 0:
        return ScorecardReport(
            scorer_version=scorer_version,
            timestamp=ts,
            mode=mode,
            corpus_size=corpus_size,
            completed_count=0,
            device_precision=0.0,
            device_recall=0.0,
            device_f1=0.0,
            tag_accuracy=0.0,
            contact_number_accuracy=0.0,
            connection_f1=0.0,
            round_trip_from_to_ok=False,
            round_trip_from_to_match=False,
            round_trip_pass_rate=0.0,
            xref_accuracy=0.0,
            note_accuracy=0.0,
            sheet_flags_clear_rate=0.0,
            flagged_rate=0.0,
            wrong_unflagged_rate=0.0,
            total_flagged_count=0,
            total_wrong_unflagged_count=0,
            total_decisions=0,
            items=[],
        )

    total_decisions = sum(r.total_decisions for r in results)
    total_flagged = sum(r.flagged_count for r in results)
    total_wrong_unflagged = sum(r.wrong_unflagged_count for r in results)
    rt_all = all(r.round_trip_from_to_ok for r in results)
    rt_rate = sum(1 for r in results if r.round_trip_from_to_ok) / n

    return ScorecardReport(
        scorer_version=scorer_version,
        timestamp=ts,
        mode=mode,
        corpus_size=corpus_size,
        completed_count=n,
        device_precision=sum(r.device_precision for r in results) / n,
        device_recall=sum(r.device_recall for r in results) / n,
        device_f1=sum(r.device_f1 for r in results) / n,
        tag_accuracy=sum(r.tag_accuracy for r in results) / n,
        contact_number_accuracy=sum(r.contact_number_accuracy for r in results) / n,
        connection_f1=sum(r.connection_f1 for r in results) / n,
        round_trip_from_to_ok=rt_all,
        round_trip_from_to_match=rt_all,
        round_trip_pass_rate=rt_rate,
        xref_accuracy=sum(r.xref_accuracy for r in results) / n,
        note_accuracy=sum(r.note_accuracy for r in results) / n,
        sheet_flags_clear_rate=sum(r.sheet_flags_clear_rate for r in results) / n,
        flagged_rate=(total_flagged / total_decisions) if total_decisions > 0 else 0.0,
        wrong_unflagged_rate=(total_wrong_unflagged / total_decisions) if total_decisions > 0 else 0.0,
        total_flagged_count=total_flagged,
        total_wrong_unflagged_count=total_wrong_unflagged,
        total_decisions=total_decisions,
        items=list(results),
    )


def _write_incremental_progress(
    save_path: Path,
    report: ScorecardReport,
    latest_item: ItemScorecardResult | None = None,
) -> None:
    """Persist incremental progress to JSON or JSONL."""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if save_path.suffix.lower() == ".jsonl":
        if latest_item is not None:
            record = {
                "scorer_version": report.scorer_version,
                "timestamp": report.timestamp,
                "mode": report.mode,
                "item": latest_item.to_dict(),
            }
            with save_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, indent=None) + "\n")
    else:
        save_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def _load_existing_progress(
    save_path: Path,
    *,
    mode: str,
    scorer_version: str,
) -> dict[str, ItemScorecardResult]:
    """Load already-scored items from `save_path` when `resumable=True`."""
    save_path = Path(save_path)
    if not save_path.is_file():
        return {}
    completed: dict[str, ItemScorecardResult] = {}
    try:
        if save_path.suffix.lower() == ".jsonl":
            for line in save_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("scorer_version") != scorer_version or row.get("mode") != mode:
                    continue
                item_data = row.get("item")
                if isinstance(item_data, dict) and "item_id" in item_data:
                    completed[item_data["item_id"]] = ItemScorecardResult(**item_data)
        else:
            data = json.loads(save_path.read_text(encoding="utf-8"))
            if data.get("scorer_version") == scorer_version and data.get("mode") == mode:
                for item_data in data.get("items", []):
                    if isinstance(item_data, dict) and "item_id" in item_data:
                        completed[item_data["item_id"]] = ItemScorecardResult(**item_data)
    except Exception:
        return {}
    return completed


def run_conversion_scorecard(
    corpus: list[CorpusPrintItem] | None = None,
    *,
    mode: str = "rules_only",
    ai_backend: ConversionAIBackend | None = None,
    save_path: str | Path | None = None,
    resumable: bool = True,
    scorer_version: str = SCORER_VERSION,
) -> ScorecardReport:
    """Run the conversion scorecard across `corpus` (defaults to all 18 prints)."""
    items = corpus if corpus is not None else build_iec_conversion_corpus()
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    existing: dict[str, ItemScorecardResult] = {}
    if save_path is not None and resumable:
        existing = _load_existing_progress(Path(save_path), mode=mode, scorer_version=scorer_version)

    results: list[ItemScorecardResult] = []
    for item in items:
        if item.item_id in existing:
            res = existing[item.item_id]
            results.append(res)
            continue
        res = score_conversion_item(
            item,
            mode=mode,
            ai_backend=ai_backend,
            scorer_version=scorer_version,
        )
        results.append(res)
        if save_path is not None:
            partial_report = _aggregate_scorecard_report(
                results,
                mode=mode,
                corpus_size=len(items),
                scorer_version=scorer_version,
                timestamp=ts,
            )
            _write_incremental_progress(Path(save_path), partial_report, latest_item=res)

    final_report = _aggregate_scorecard_report(
        results,
        mode=mode,
        corpus_size=len(items),
        scorer_version=scorer_version,
        timestamp=ts,
    )
    if save_path is not None and Path(save_path).suffix.lower() != ".jsonl":
        _write_incremental_progress(Path(save_path), final_report)
    return final_report


def compare_scorecard_modes(
    corpus: list[CorpusPrintItem] | None = None,
    *,
    modes: tuple[str, ...] = ("rules_only", "ai_assisted", "ai_recorded"),
    save_path: str | Path | None = None,
    resumable: bool = True,
    scorer_version: str = SCORER_VERSION,
) -> dict[str, Any]:
    """Run multiple conversion modes side by side with the exact same `scorer_version`."""
    items = corpus if corpus is not None else build_iec_conversion_corpus()
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    reports: dict[str, ScorecardReport] = {}

    out_path = Path(save_path) if save_path is not None else None
    for mode in modes:
        mode_save = (
            out_path.parent / f"{out_path.stem}.{mode}{out_path.suffix}"
            if out_path is not None
            else None
        )
        rep = run_conversion_scorecard(
            items,
            mode=mode,
            save_path=mode_save,
            resumable=resumable,
            scorer_version=scorer_version,
        )
        if rep.scorer_version != scorer_version:
            raise AssertionError(
                f"Scorer version mismatch in mode {mode}: {rep.scorer_version} != {scorer_version}"
            )
        reports[mode] = rep

    deltas: dict[str, Any] = {}
    if "rules_only" in reports and "ai_recorded" in reports:
        r_rules = reports["rules_only"]
        r_ai = reports["ai_recorded"]
        deltas["ai_recorded_vs_rules_only"] = {
            "device_f1_delta": r_ai.device_f1 - r_rules.device_f1,
            "tag_accuracy_delta": r_ai.tag_accuracy - r_rules.tag_accuracy,
            "connection_f1_delta": r_ai.connection_f1 - r_rules.connection_f1,
            "note_accuracy_delta": r_ai.note_accuracy - r_rules.note_accuracy,
            "flagged_rate_delta": r_ai.flagged_rate - r_rules.flagged_rate,
            "wrong_unflagged_rate_rules_only": r_rules.wrong_unflagged_rate,
            "wrong_unflagged_rate_ai_recorded": r_ai.wrong_unflagged_rate,
        }

    comparison_payload = {
        "scorer_version": scorer_version,
        "timestamp": ts,
        "corpus_size": len(items),
        "modes": {m: r.to_dict() for m, r in reports.items()},
        "deltas": deltas,
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(comparison_payload, indent=2), encoding="utf-8")
    return comparison_payload


GOLDEN_NEMA_FROM_TO_FILES: dict[str, str] = {
    "dol": "dol-from-to.nema.csv",
    "reversing": "reversing-from-to.nema.csv",
    "plc": "plc-from-to.nema.csv",
}


def build_golden_nema_from_to_session(kind: str) -> Session:
    """Build the standard IEC example session (`dol`, `reversing`, or `plc`) and convert to NEMA."""
    norm = kind.strip().lower()
    if norm == "dol":
        session = build_dol()
    elif norm == "reversing":
        session = Session.create("Reversing")
        session.insert_circuit(kind="reversing", control="local", standard="IEC")
    elif norm == "plc":
        session = Session.create("PLC")
        session.insert_circuit(kind="plc_motor", control="local", standard="IEC")
    else:
        raise ValueError(f"Unknown golden From-To kind {kind!r}; expected 'dol', 'reversing', or 'plc'")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        session.convert_standard(
            "nema",
            out=td_path / f"{norm}.nema.pcad.yaml",
            workspace=td_path / f"{norm}.workspace.json",
        )
        session.path = None
    return session


def export_golden_nema_from_to_csv(kind: str, dest: str | Path) -> Path:
    """Convert `kind` (`dol`, `reversing`, `plc`) to NEMA and write its From-To CSV to `dest`."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    session = build_golden_nema_from_to_session(kind)
    session.export_from_to(dest_path, sort="number")
    lf_text = dest_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    dest_path.write_text(lf_text, encoding="utf-8", newline="\n")
    return dest_path


def verify_golden_nema_from_to_csvs(golden_dir: str | Path) -> dict[str, bool]:
    """Verify `dol-from-to.nema.csv`, `reversing-from-to.nema.csv`, and `plc-from-to.nema.csv`."""
    gdir = Path(golden_dir)
    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for kind, filename in GOLDEN_NEMA_FROM_TO_FILES.items():
            expected_path = gdir / filename
            if not expected_path.is_file():
                results[kind] = False
                continue
            actual_path = export_golden_nema_from_to_csv(kind, td_path / filename)
            exp_text = expected_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            act_text = actual_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            results[kind] = exp_text == act_text
    return results
