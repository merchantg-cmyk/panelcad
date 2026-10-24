"""Structured conversion plan, chunks, and decision records for IEC <-> NEMA conversion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from panelcad.convert.table import parse_device_tag
from panelcad.core.models import Project

DECISION_REASON_CODES: frozenset[str] = frozenset(
    {
        "ambiguous_role",
        "multifunction_block",
        "unmapped_symbol",
        "unmapped_pin",
        "off_grid_relayout",
        "tag_collision",
        "coil_without_contacts",
        "contact_without_coil",
        "multipage_consolidation",
        "note_reworded",
        "layout_candidate",
        "imported_low_confidence",
    }
)

SHEET_FLAG_REASON_CODES: frozenset[str] = frozenset(
    {
        "component_overlap",
        "rail_collision",
        "wire_overlap",
        "title_block_overlap",
    }
)

ALL_REASON_CODES: frozenset[str] = DECISION_REASON_CODES | SHEET_FLAG_REASON_CODES


@dataclass
class ConversionDecision:
    """One structured conversion decision or geometry review item."""

    reason_code: str
    component_id: str | None = None
    sheet_id: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = True
    message: str = ""
    chunk_id: str = ""
    severity: str = "warning"
    model: str = ""
    confidence: float | None = None
    proposal: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    chunk: str = ""
    kind: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.note and not self.message:
            self.message = self.note
        elif self.message and not self.note:
            self.note = self.message
        if self.chunk and not self.chunk_id:
            self.chunk_id = self.chunk
        elif self.chunk_id and not self.chunk:
            self.chunk = self.chunk_id

    @property
    def flag(self) -> str:
        return self.reason_code

    @property
    def detail(self) -> str:
        return self.message

    @property
    def ref(self) -> str:
        return self.component_id or ""

    @property
    def sheet(self) -> str:
        return self.sheet_id or ""

    @property
    def action(self) -> str:
        return self.reason_code

    @property
    def accepted(self) -> bool:
        if isinstance(self.proposal, dict) and "accepted" in self.proposal:
            return bool(self.proposal["accepted"])
        return not self.needs_review

    @accepted.setter
    def accepted(self, value: bool) -> None:
        if not isinstance(self.proposal, dict):
            self.proposal = {"value": self.proposal}
        self.proposal["accepted"] = bool(value)

    @property
    def original(self) -> Any:
        if "original" in self.before:
            return self.before["original"]
        if "text" in self.before:
            return self.before["text"]
        if "original" in self.proposal:
            return self.proposal["original"]
        return self.before

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reason_code": str(self.reason_code),
            "component_id": self.component_id,
            "sheet_id": self.sheet_id,
            "before": dict(self.before or {}),
            "after": dict(self.after or {}),
            "needs_review": bool(self.needs_review),
            "accepted": bool(self.accepted),
            "message": str(self.message or ""),
            "chunk_id": str(self.chunk_id or ""),
            "severity": str(self.severity or "warning"),
            "flag": str(self.reason_code),
            "detail": str(self.message or ""),
            "ref": str(self.component_id or ""),
            "sheet": str(self.sheet_id or ""),
        }
        if self.model:
            out["model"] = str(self.model)
        if self.confidence is not None:
            out["confidence"] = float(self.confidence)
        if self.proposal:
            out["proposal"] = dict(self.proposal)
        if self.reason:
            out["reason"] = str(self.reason)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ConversionDecision") -> "ConversionDecision":
        if isinstance(data, ConversionDecision):
            return data
        reason = str(data.get("reason_code") or data.get("flag") or "needs_review")
        comp_id = data.get("component_id")
        if comp_id is None and data.get("ref"):
            comp_id = str(data["ref"])
        sh_id = data.get("sheet_id")
        if sh_id is None and data.get("sheet"):
            sh_id = str(data["sheet"])
        msg = str(data.get("message") or data.get("detail") or data.get("note") or "")
        raw_conf = data.get("confidence")
        conf_val: float | None = None
        if raw_conf is not None:
            try:
                conf_val = float(raw_conf)
            except (TypeError, ValueError):
                conf_val = None
        return cls(
            reason_code=reason,
            component_id=str(comp_id) if comp_id else None,
            sheet_id=str(sh_id) if sh_id else None,
            before=dict(data.get("before") or {}),
            after=dict(data.get("after") or {}),
            needs_review=bool(data.get("needs_review", True)),
            message=msg,
            chunk_id=str(data.get("chunk_id") or data.get("chunk") or ""),
            severity=str(data.get("severity") or "warning"),
            model=str(data.get("model") or ""),
            confidence=conf_val,
            proposal={
                **dict(data.get("proposal") or {}),
                **({"accepted": bool(data["accepted"])} if "accepted" in data else {}),
            },
            reason=str(data.get("reason") or ""),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass
class ConversionChunk:
    """One reviewable conversion step (tags, per-sheet symbols, or per-sheet layout)."""

    id: str
    kind: str
    sheet: str = ""
    status: str = "pending"
    decisions: list[ConversionDecision] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    suggestion: str = ""
    tag_bumps: list[dict[str, str]] = field(default_factory=list)

    @property
    def reason_codes(self) -> list[str]:
        seen: list[str] = []
        for dec in self.decisions:
            code = dec.reason_code if isinstance(dec, ConversionDecision) else str(dec.get("reason_code") or dec.get("flag") or "")
            if code and code not in seen:
                seen.append(code)
        for issue in self.issues:
            code = str(issue.get("flag") or issue.get("reason_code") or "")
            if code and code not in seen:
                seen.append(code)
        return seen

    @property
    def needs_review(self) -> bool:
        if self.status == "accepted":
            return False
        return any(
            (d.needs_review if isinstance(d, ConversionDecision) else bool(d.get("needs_review", True)))
            for d in self.decisions
        )

    def to_dict(self) -> dict[str, Any]:
        decs = [
            d.to_dict() if isinstance(d, ConversionDecision) else ConversionDecision.from_dict(d).to_dict()
            for d in self.decisions
        ]
        return {
            "id": self.id,
            "kind": self.kind,
            "sheet": self.sheet,
            "status": self.status,
            "decisions": decs,
            "reason_codes": self.reason_codes,
            "needs_review": self.needs_review,
            "issues": list(self.issues),
            "suggestion": self.suggestion,
            "tag_bumps": list(self.tag_bumps),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ConversionChunk") -> "ConversionChunk":
        if isinstance(data, ConversionChunk):
            return data
        raw_decs = data.get("decisions")
        if raw_decs is not None:
            decs = [ConversionDecision.from_dict(d) for d in raw_decs]
        else:
            decs = decisions_from_issues(
                list(data.get("issues") or []),
                sheet_id=str(data.get("sheet") or "") or None,
                chunk_id=str(data.get("id") or ""),
            )
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or ""),
            sheet=str(data.get("sheet") or ""),
            status=str(data.get("status") or "pending"),
            decisions=decs,
            issues=list(data.get("issues") or []),
            suggestion=str(data.get("suggestion") or ""),
            tag_bumps=list(data.get("tag_bumps") or []),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass
class ConversionPlan:
    """Complete conversion plan and live workspace state."""

    workspace: str
    out_path: str
    target: str
    source_standard: str
    source_path: str = ""
    sheets: list[str] = field(default_factory=list)
    whole_project: bool = True
    tag_map: dict[str, str] = field(default_factory=dict)
    planned_tag_map: dict[str, str] = field(default_factory=dict)
    pin_rewrites: list[dict[str, str]] = field(default_factory=list)
    unmapped_preview: list[dict[str, Any]] = field(default_factory=list)
    rail_map: dict[str, dict[str, str]] = field(default_factory=dict)
    unmapped: list[dict[str, Any]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    done: bool = False
    chunks: list[ConversionChunk] = field(default_factory=list)
    decisions: list[ConversionDecision] = field(default_factory=list)
    connection_check: dict[str, Any] = field(default_factory=dict)
    mode: str = "rules_only"
    confidence_threshold: float = 0.80

    def needs_review_decisions(self) -> list[ConversionDecision]:
        accepted_chunks = {c.id for c in self.chunks if c.status == "accepted"}
        out: list[ConversionDecision] = []
        for dec in self.decisions:
            d = ConversionDecision.from_dict(dec)
            if d.needs_review and (not d.chunk_id or d.chunk_id not in accepted_chunks):
                out.append(d)
        return out

    @property
    def reason_codes(self) -> list[str]:
        seen: list[str] = []
        for dec in self.decisions:
            d = ConversionDecision.from_dict(dec)
            if d.reason_code and d.reason_code not in seen:
                seen.append(d.reason_code)
        return seen

    def to_dict(self) -> dict[str, Any]:
        chunk_dicts = [
            c.to_dict() if isinstance(c, ConversionChunk) else ConversionChunk.from_dict(c).to_dict()
            for c in self.chunks
        ]
        dec_dicts = [
            d.to_dict() if isinstance(d, ConversionDecision) else ConversionDecision.from_dict(d).to_dict()
            for d in self.decisions
        ]
        out: dict[str, Any] = {
            "workspace": self.workspace,
            "out_path": self.out_path,
            "target": self.target,
            "source_standard": self.source_standard,
            "source_path": self.source_path,
            "sheets": list(self.sheets),
            "whole_project": bool(self.whole_project),
            "tag_map": dict(self.tag_map),
            "planned_tag_map": dict(self.planned_tag_map),
            "pin_rewrites": list(self.pin_rewrites),
            "unmapped_preview": list(self.unmapped_preview),
            "rail_map": dict(self.rail_map),
            "unmapped": list(self.unmapped),
            "pending": list(self.pending),
            "done": bool(self.done),
            "chunks": chunk_dicts,
            "decisions": dec_dicts,
            "reason_codes": self.reason_codes,
            "connection_check": dict(self.connection_check),
        }
        if self.mode and self.mode != "rules_only":
            out["mode"] = self.mode
            out["confidence_threshold"] = float(self.confidence_threshold)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ConversionPlan") -> "ConversionPlan":
        if isinstance(data, ConversionPlan):
            return data
        chunks = [ConversionChunk.from_dict(c) for c in (data.get("chunks") or [])]
        raw_decs = data.get("decisions")
        if raw_decs is not None:
            decisions = [ConversionDecision.from_dict(d) for d in raw_decs]
        else:
            decisions = [dec for c in chunks for dec in c.decisions]
        return cls(
            workspace=str(data.get("workspace") or ""),
            out_path=str(data.get("out_path") or ""),
            target=str(data.get("target") or ""),
            source_standard=str(data.get("source_standard") or ""),
            source_path=str(data.get("source_path") or ""),
            sheets=list(data.get("sheets") or []),
            whole_project=bool(data.get("whole_project", True)),
            tag_map=dict(data.get("tag_map") or {}),
            planned_tag_map=dict(data.get("planned_tag_map") or {}),
            pin_rewrites=list(data.get("pin_rewrites") or []),
            unmapped_preview=list(data.get("unmapped_preview") or []),
            rail_map=dict(data.get("rail_map") or {}),
            unmapped=list(data.get("unmapped") or []),
            pending=list(data.get("pending") or []),
            done=bool(data.get("done", False)),
            chunks=chunks,
            decisions=decisions,
            connection_check=dict(data.get("connection_check") or {}),
            mode=str(data.get("mode") or "rules_only"),
            confidence_threshold=float(data.get("confidence_threshold", 0.80) or 0.80),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def values(self):
        return self.to_dict().values()


def decisions_from_issues(
    issues: list[dict[str, Any]],
    *,
    sheet_id: str | None = None,
    chunk_id: str = "",
) -> list[ConversionDecision]:
    """Convert raw chunk issue dicts into structured ``ConversionDecision`` records."""

    out: list[ConversionDecision] = []
    for issue in issues:
        raw_flag = str(issue.get("reason_code") or issue.get("flag") or "needs_review")
        code = "off_grid_relayout" if raw_flag == "unplaced" else raw_flag
        comp_id = issue.get("component_id") or issue.get("ref")
        sh_id = issue.get("sheet_id") or issue.get("sheet") or sheet_id
        msg = str(issue.get("message") or issue.get("detail") or "")
        before = dict(issue.get("before") or {})
        after = dict(issue.get("after") or {})
        if issue.get("library_id") and "library_id" not in before:
            before["library_id"] = str(issue["library_id"])
        raw_conf = issue.get("confidence")
        conf_val = float(raw_conf) if raw_conf is not None else None
        out.append(
            ConversionDecision(
                reason_code=code,
                component_id=str(comp_id) if comp_id else None,
                sheet_id=str(sh_id) if sh_id else None,
                before=before,
                after=after,
                needs_review=bool(issue.get("needs_review", True)),
                message=msg,
                chunk_id=chunk_id or str(issue.get("chunk_id") or ""),
                severity=str(issue.get("severity") or "warning"),
                model=str(issue.get("model") or ""),
                confidence=conf_val,
                proposal=dict(issue.get("proposal") or {}),
                reason=str(issue.get("reason") or ""),
            )
        )
    return out


_AMBIGUOUS_IEC_PREFIXES = {"K", "Q", "S", "F"}


def decisions_for_tags_chunk(
    session: Any,
    ws: dict[str, Any],
    chunk: dict[str, Any],
) -> list[ConversionDecision]:
    """Build structured decisions for the ``tags`` chunk."""

    chunk_id = str(chunk.get("id") or "tags")
    decisions: list[ConversionDecision] = []
    tag_map = {str(k): str(v) for k, v in (chunk.get("tag_map") or {}).items()}
    src_data = ws.get("source_project")
    src_proj = Project.model_validate(src_data) if isinstance(src_data, dict) else session.project
    selected = set(ws.get("sheets") or [s.id for s in src_proj.sheets])

    # Include any AI-assisted tag/note decisions already attached to the chunk
    ai_handled_components: set[str] = set()
    for raw_ai_dec in chunk.get("ai_decisions") or []:
        dec_obj = ConversionDecision.from_dict(raw_ai_dec)
        if not dec_obj.chunk_id:
            dec_obj.chunk_id = chunk_id
        decisions.append(dec_obj)
        if dec_obj.reason_code == "ambiguous_role":
            orig_id = str(dec_obj.before.get("id") or dec_obj.component_id or "")
            if orig_id:
                p_orig = parse_device_tag(orig_id)
                ai_handled_components.add(f"{p_orig[0]}{p_orig[1]}" if p_orig else orig_id)
            if dec_obj.component_id:
                ai_handled_components.add(dec_obj.component_id)

    # 1. Tag collisions from tag_bumps
    for row in chunk.get("tag_bumps") or []:
        old_tag = str(row.get("from") or "")
        new_tag = str(row.get("to") or "")
        sh_id = _find_component_sheet(src_proj, old_tag)
        decisions.append(
            ConversionDecision(
                reason_code="tag_collision",
                component_id=new_tag or old_tag or None,
                sheet_id=sh_id,
                before={"tag": old_tag},
                after={"tag": new_tag},
                needs_review=True,
                message=(
                    f"{old_tag} could not take the next number, so it is now {new_tag}. "
                    "Another device on the drawing already uses that number."
                ),
                chunk_id=chunk_id,
            )
        )

    # 2. Ambiguous role & multifunction block detection across source components
    seen_families: set[str] = set(ai_handled_components)
    for sheet in src_proj.sheets:
        if sheet.id not in selected:
            continue
        for comp in sheet.components:
            parsed = parse_device_tag(comp.id)
            prefix = parsed[0].upper() if parsed else ""
            base = f"{parsed[0]}{parsed[1]}" if parsed else comp.id
            lib_id = str(comp.library_id or "")
            low_lib = lib_id.lower()
            role = str(comp.properties.get("role") or "").strip()
            new_id = tag_map.get(comp.id, comp.id)

            if _is_multifunction_component(session, comp):
                decisions.append(
                    ConversionDecision(
                        reason_code="multifunction_block",
                        component_id=new_id,
                        sheet_id=sheet.id,
                        before={"id": comp.id, "library_id": lib_id, "role": role},
                        after={"id": new_id, "library_id": lib_id},
                        needs_review=True,
                        message=(
                            f"{comp.id} ({lib_id}) combines multiple device functions in one block; "
                            f"verify tag {new_id} and terminal assignments in {ws.get('target')}."
                        ),
                        chunk_id=chunk_id,
                    )
                )

            if base in seen_families or comp.id in seen_families or new_id in seen_families:
                continue
            if prefix in _AMBIGUOUS_IEC_PREFIXES and not role and _is_ambiguous_symbol(session, lib_id):
                seen_families.add(base)
                decisions.append(
                    ConversionDecision(
                        reason_code="ambiguous_role",
                        component_id=new_id,
                        sheet_id=sheet.id,
                        before={"id": comp.id, "prefix": prefix, "library_id": lib_id, "role": ""},
                        after={"id": new_id},
                        needs_review=True,
                        message=(
                            f"{comp.id} uses shared prefix '{prefix}' without an explicit role; "
                            f"inferred {new_id} from '{lib_id}'. Confirm the device role."
                        ),
                        chunk_id=chunk_id,
                    )
                )
            elif "generic" in low_lib or "custom" in low_lib or "unknown" in low_lib:
                seen_families.add(base)
                decisions.append(
                    ConversionDecision(
                        reason_code="ambiguous_role",
                        component_id=new_id,
                        sheet_id=sheet.id,
                        before={"id": comp.id, "library_id": lib_id, "role": role},
                        after={"id": new_id},
                        needs_review=True,
                        message=(
                            f"{comp.id} ({lib_id}) has an ambiguous device role; "
                            f"verify converted tag {new_id}."
                        ),
                        chunk_id=chunk_id,
                    )
                )

    return _dedupe_decisions(decisions)


def decisions_for_symbols_chunk(
    session: Any,
    ws: dict[str, Any],
    chunk: dict[str, Any],
) -> list[ConversionDecision]:
    """Build structured decisions for a ``<sheet>:symbols`` chunk."""

    chunk_id = str(chunk.get("id") or "")
    sheet_id = str(chunk.get("sheet") or "")
    decisions: list[ConversionDecision] = []
    for raw_ai_dec in chunk.get("ai_decisions") or []:
        dec_obj = ConversionDecision.from_dict(raw_ai_dec)
        if not dec_obj.chunk_id:
            dec_obj.chunk_id = chunk_id
        decisions.append(dec_obj)

    ai_unmapped_Ids = {
        str(d.component_id or "")
        for d in decisions
        if d.reason_code == "unmapped_symbol" and d.component_id
    }
    for dec in decisions_from_issues(
        list(chunk.get("issues") or []),
        sheet_id=sheet_id or None,
        chunk_id=chunk_id,
    ):
        if dec.reason_code == "unmapped_symbol" and (dec.component_id or "") in ai_unmapped_Ids:
            continue
        decisions.append(dec)

    # Check for orphan coils or contacts on this sheet
    try:
        sheet = session.sheet(sheet_id)
    except Exception:  # noqa: BLE001
        return _dedupe_decisions(decisions)

    coil_bases, contact_bases, power_bases = _collect_project_coil_and_contact_bases(session.project)
    for comp in sheet.components:
        lid = str(comp.library_id or "").lower()
        parsed = parse_device_tag(comp.id)
        base = f"{parsed[0]}{parsed[1]}" if parsed else comp.id
        is_contact = lid.endswith((".aux_no", ".aux_nc", ".ol_contact")) or ".timers.contact_" in lid
        is_coil = (
            lid.endswith(".coil")
            or ".relays." in lid
            or ".timers.on_delay" in lid
            or ".timers.off_delay" in lid
        )
        parent_ref = str(comp.properties.get("parent") or base)
        parent_parsed = parse_device_tag(parent_ref)
        parent_base = f"{parent_parsed[0]}{parent_parsed[1]}" if parent_parsed else parent_ref

        if is_contact and parent_base not in coil_bases and parent_base not in power_bases:
            decisions.append(
                ConversionDecision(
                    reason_code="contact_without_coil",
                    component_id=comp.id,
                    sheet_id=sheet.id,
                    before={"id": comp.id, "parent": parent_base},
                    after={"id": comp.id, "library_id": comp.library_id},
                    needs_review=True,
                    message=(
                        f"{comp.id} on sheet {sheet.id} references parent '{parent_base}', "
                        "which has no matching coil or power device in the project."
                    ),
                    chunk_id=chunk_id,
                )
            )
        elif is_coil and base not in contact_bases and base not in power_bases:
            if bool(comp.properties.get("flag_orphan_coil")) or str(comp.properties.get("role") or "") in {
                "relay",
                "contactor",
                "starter_coil",
            }:
                decisions.append(
                    ConversionDecision(
                        reason_code="coil_without_contacts",
                        component_id=comp.id,
                        sheet_id=sheet.id,
                        before={"id": comp.id},
                        after={"id": comp.id, "library_id": comp.library_id},
                        needs_review=True,
                        message=(
                            f"Coil {comp.id} on sheet {sheet.id} has no associated contacts "
                            "or power poles in the project."
                        ),
                        chunk_id=chunk_id,
                    )
                )

    return _dedupe_decisions(decisions)


def decisions_for_layout_chunk(
    session: Any,
    ws: dict[str, Any],
    chunk: dict[str, Any],
) -> list[ConversionDecision]:
    """Build structured decisions for a ``<sheet>:layout`` chunk."""

    chunk_id = str(chunk.get("id") or "")
    sheet_id = str(chunk.get("sheet") or "")
    decisions: list[ConversionDecision] = []
    for raw_ai_dec in chunk.get("ai_decisions") or []:
        dec_obj = ConversionDecision.from_dict(raw_ai_dec)
        if not dec_obj.chunk_id:
            dec_obj.chunk_id = chunk_id
        decisions.append(dec_obj)

    decisions.extend(
        decisions_from_issues(
            list(chunk.get("issues") or []),
            sheet_id=sheet_id or None,
            chunk_id=chunk_id,
        )
    )

    rail_rec = chunk.get("rail") or {}
    for ref in rail_rec.get("unplaced") or []:
        decisions.append(
            ConversionDecision(
                reason_code="off_grid_relayout",
                component_id=str(ref),
                sheet_id=sheet_id or None,
                before={"sheet_id": sheet_id, "ref": str(ref)},
                after={"placed_on_ladder": False},
                needs_review=True,
                message=f"{ref} is wired into a rung but was not placed on the ladder grid. Check its placement.",
                chunk_id=chunk_id,
            )
        )

    block = session.project.properties.get("convert_standard") or {}
    mp = block.get("multipage") if isinstance(block, dict) else None
    if isinstance(mp, dict):
        consolidated_from = list(mp.get("consolidated_from") or [])
        kept = list(mp.get("kept_sheets") or [])
        removed = list(mp.get("removed_sheets") or [])
        target_map = dict(mp.get("target_for_sheet") or {})
        if sheet_id in consolidated_from and len(consolidated_from) >= 2:
            dest_sheet = str(target_map.get(sheet_id) or (kept[0] if kept else sheet_id))
            decisions.append(
                ConversionDecision(
                    reason_code="multipage_consolidation",
                    component_id=None,
                    sheet_id=dest_sheet,
                    before={"sheets": consolidated_from, "source_sheet": sheet_id},
                    after={"kept_sheets": kept, "removed_sheets": removed, "target_sheet": dest_sheet},
                    needs_review=False,
                    message=(
                        f"Consolidated multi-page control sheets {', '.join(consolidated_from)} "
                        f"onto NEMA ladder sheet {dest_sheet}."
                    ),
                    chunk_id=chunk_id,
                    severity="info",
                )
            )

    return _dedupe_decisions(decisions)


def sync_project_conversion_decisions(project: Project, ws: dict[str, Any]) -> list[dict[str, Any]]:
    """Persist unresolved ``needs_review`` (and unaccepted AI-assisted) conversion decisions onto the project metadata."""

    unresolved: list[dict[str, Any]] = []
    all_decisions: list[dict[str, Any]] = []
    for chunk in ws.get("chunks") or []:
        status = str(chunk.get("status") or "pending")
        if status not in {"done", "accepted"}:
            continue
        chunk_id = str(chunk.get("id") or "")
        sheet_id = str(chunk.get("sheet") or "") or None
        raw_decs = chunk.get("decisions")
        if raw_decs is not None:
            decs = [ConversionDecision.from_dict(d) for d in raw_decs]
        else:
            decs = decisions_from_issues(
                list(chunk.get("issues") or []),
                sheet_id=sheet_id,
                chunk_id=chunk_id,
            )
        for dec in decs:
            if not dec.chunk_id:
                dec.chunk_id = chunk_id
            row = dec.to_dict()
            all_decisions.append(row)
            is_resolved = isinstance(dec.proposal, dict) and bool(dec.proposal.get("resolved"))
            if status != "accepted" and not is_resolved and (dec.needs_review or bool(dec.model)):
                unresolved.append(row)

    raw_imported = project.properties.get("imported_from_to_decisions") or ws.get("imported_decisions") or []
    seen_keys = {
        (
            str(d.get("reason_code") or ""),
            str(d.get("component_id") or ""),
            str(d.get("sheet_id") or ""),
            str(d.get("message") or ""),
        )
        for d in all_decisions
    }
    for raw_imp in raw_imported:
        imp_dec = ConversionDecision.from_dict(raw_imp)
        if not imp_dec.chunk_id:
            imp_dec.chunk_id = "import"
        row = imp_dec.to_dict()
        key = (
            str(row.get("reason_code") or ""),
            str(row.get("component_id") or ""),
            str(row.get("sheet_id") or ""),
            str(row.get("message") or ""),
        )
        if key not in seen_keys:
            seen_keys.add(key)
            all_decisions.append(row)
            if imp_dec.needs_review or bool(imp_dec.model):
                unresolved.append(row)

    ws["decisions"] = all_decisions
    if unresolved:
        project.properties["last_conversion_decisions"] = unresolved
    else:
        project.properties.pop("last_conversion_decisions", None)

    block = project.properties.get("convert_standard")
    if isinstance(block, dict):
        block["decisions"] = all_decisions
        block["unresolved_decisions"] = unresolved
    return unresolved


def _find_component_sheet(project: Project, comp_id: str) -> str | None:
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id == comp_id:
                return sheet.id
    return None


def _is_multifunction_component(session: Any, comp: Any) -> bool:
    props = getattr(comp, "properties", None) or {}
    if props.get("multifunction") or props.get("combined_roles") or props.get("functions"):
        return True
    lib_id = str(getattr(comp, "library_id", "") or "").lower()
    if "multifunction" in lib_id or "combo" in lib_id:
        return True
    return False


def _is_ambiguous_symbol(session: Any, library_id: str) -> bool:
    low = str(library_id or "").lower()
    if not low:
        return True
    if any(tok in low for tok in ("generic", "unknown", "custom", "blackbox", "box")):
        return True
    try:
        dev = session.catalog.get(library_id)
    except Exception:  # noqa: BLE001
        return True
    cat = str(getattr(dev, "category", "") or "").lower()
    return not cat or cat in {"misc", "generic", "custom"}


def _collect_project_coil_and_contact_bases(project: Project) -> tuple[set[str], set[str], set[str]]:
    coils: set[str] = set()
    contacts: set[str] = set()
    power_devs: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            lid = str(comp.library_id or "").lower()
            parsed = parse_device_tag(comp.id)
            base = f"{parsed[0]}{parsed[1]}" if parsed else comp.id
            parent_ref = str(comp.properties.get("parent") or base)
            p_parsed = parse_device_tag(parent_ref)
            parent_base = f"{p_parsed[0]}{p_parsed[1]}" if p_parsed else parent_ref
            if lid.endswith((".aux_no", ".aux_nc", ".ol_contact")) or ".timers.contact_" in lid:
                contacts.add(parent_base)
            elif (
                lid.endswith(".coil")
                or ".relays." in lid
                or ".timers.on_delay" in lid
                or ".timers.off_delay" in lid
            ):
                coils.add(base)
            elif any(tok in lid for tok in (".contactors.ac3", ".protection.overload", ".breakers.", ".starters.")):
                power_devs.add(base)
    return coils, contacts, power_devs


def _dedupe_decisions(decisions: list[ConversionDecision]) -> list[ConversionDecision]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[ConversionDecision] = []
    for dec in decisions:
        key = (dec.reason_code, dec.component_id or "", dec.sheet_id or "", dec.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(dec)
    return out



def decision_original_value(decision: ConversionDecision | dict[str, Any]) -> str:
    """Return the concise original value string (tag, library_id, or note text) for a decision."""

    dec = ConversionDecision.from_dict(decision)
    code = str(dec.reason_code or "")
    before = dec.before if isinstance(dec.before, dict) else {}
    prop = dec.proposal if isinstance(dec.proposal, dict) else {}

    if code in {"ambiguous_role", "tag_collision", "multifunction_block", "coil_without_contacts", "contact_without_coil"}:
        return str(before.get("id") or before.get("tag") or prop.get("original") or dec.component_id or "")
    if code == "unmapped_symbol" or dec.kind == "symbol_swap":
        return str(before.get("library_id") or prop.get("original") or "")
    if code == "note_reworded" or dec.kind == "note_reword":
        return str(before.get("original") or before.get("text") or prop.get("original") or "")
    if code == "layout_candidate":
        cands = before.get("candidates")
        if isinstance(cands, list) and cands:
            first = cands[0]
            if isinstance(first, dict):
                return str(first.get("id") or first.get("candidate_id") or "candidate_0")
            return str(first)
        return str(before.get("candidate_id") or "ladder_standard")
    for key in ("id", "tag", "library_id", "original", "text", "ref"):
        if before.get(key):
            return str(before[key])
    return str(dec.component_id or "")


def decision_proposal_value(decision: ConversionDecision | dict[str, Any]) -> str:
    """Return the concise proposed/current value string (tag, library_id, or note text) for a decision."""

    dec = ConversionDecision.from_dict(decision)
    code = str(dec.reason_code or "")
    after = dec.after if isinstance(dec.after, dict) else {}
    prop = dec.proposal if isinstance(dec.proposal, dict) else {}

    if code in {"ambiguous_role", "tag_collision", "multifunction_block", "coil_without_contacts", "contact_without_coil"}:
        return str(
            prop.get("chosen_option")
            or prop.get("id")
            or after.get("id")
            or after.get("tag")
            or prop.get("prefix")
            or dec.component_id
            or decision_original_value(dec)
        )
    if code == "unmapped_symbol" or dec.kind == "symbol_swap":
        return str(
            prop.get("chosen_option")
            or prop.get("library_id")
            or after.get("library_id")
            or decision_original_value(dec)
        )
    if code == "note_reworded" or dec.kind == "note_reword":
        return str(
            prop.get("chosen_option")
            or prop.get("text")
            or prop.get("proposal")
            or after.get("text")
            or decision_original_value(dec)
        )
    if code == "layout_candidate":
        return str(
            prop.get("chosen_option")
            or prop.get("candidate_id")
            or after.get("candidate_id")
            or decision_original_value(dec)
        )
    for key in ("chosen_option", "id", "tag", "library_id", "text", "proposal"):
        if prop.get(key):
            return str(prop[key])
        if after.get(key):
            return str(after[key])
    return decision_original_value(dec)


def sort_decisions_by_confidence(
    decisions: list[ConversionDecision | dict[str, Any]],
) -> list[ConversionDecision]:
    """Return decisions sorted by confidence ascending (lowest confidence / needs_review=True first)."""

    objs = [ConversionDecision.from_dict(d) for d in (decisions or [])]

    def _sort_key(d: ConversionDecision) -> tuple[float, int, str, str, str]:
        conf = float(d.confidence) if d.confidence is not None else (0.0 if d.needs_review else 1.0)
        review_rank = 0 if d.needs_review else 1
        return (conf, review_rank, str(d.sheet_id or ""), str(d.component_id or ""), str(d.reason_code or ""))

    return sorted(objs, key=_sort_key)


def valid_decision_options(
    decision: ConversionDecision | dict[str, Any],
    *,
    target: str = "NEMA/ANSI",
    project: Project | None = None,
    catalog: Any | None = None,
) -> list[str]:
    """Return strictly valid option strings for the decision picker in CompareDialog."""

    from panelcad.convert.ai_assist import _default_pin_bridge
    from panelcad.convert.table import ROLE_PREFIX_TABLE, parse_device_tag, target_standard
    from panelcad.core.refs import normalize_standard

    dec = ConversionDecision.from_dict(decision)
    tgt = target_standard(target or "NEMA/ANSI")
    code = str(dec.reason_code or "")
    orig_val = decision_original_value(dec)
    prop_val = decision_proposal_value(dec)
    before = dec.before if isinstance(dec.before, dict) else {}

    # 1. Role / tag decisions: valid role prefixes and concrete tags from ROLE_PREFIX_TABLE
    if code in {"ambiguous_role", "tag_collision", "multifunction_block"} or dec.kind == "tag_rename":
        prefix_key = "nema_prefix" if tgt == "NEMA/ANSI" else "iec_prefix"
        alias_key = "iec_aliases" if tgt == "NEMA/ANSI" else "nema_aliases"
        src_pfx_key = "iec_prefix" if tgt == "NEMA/ANSI" else "nema_prefix"

        parsed_orig = parse_device_tag(orig_val) if orig_val else None
        src_prefix = str(before.get("prefix") or (parsed_orig[0] if parsed_orig else "")).upper()
        num_str = str(parsed_orig[1]) if (parsed_orig and parsed_orig[1] is not None) else "1"
        suffix_str = str(parsed_orig[2] or "") if parsed_orig else ""

        preferred_prefixes: list[str] = []
        other_prefixes: list[str] = []
        for row in ROLE_PREFIX_TABLE:
            tgt_pfx = str(row.get(prefix_key) or "").strip()
            if not tgt_pfx:
                continue
            aliases = {str(a).upper() for a in (row.get(alias_key) or ())} | {str(row.get(src_pfx_key) or "").upper()}
            if src_prefix and src_prefix in aliases:
                if tgt_pfx not in preferred_prefixes:
                    preferred_prefixes.append(tgt_pfx)
            else:
                if tgt_pfx not in other_prefixes and tgt_pfx not in preferred_prefixes:
                    other_prefixes.append(tgt_pfx)

        all_prefixes = preferred_prefixes + [p for p in other_prefixes if p not in preferred_prefixes]
        options: list[str] = []

        # Include concrete numbered tags first for preferred prefixes, then prefixes themselves
        for pfx in preferred_prefixes:
            concrete = f"{pfx}{num_str}{suffix_str}"
            if concrete not in options:
                options.append(concrete)
        if prop_val and prop_val not in options:
            parsed_prop = parse_device_tag(prop_val)
            if parsed_prop and parsed_prop[0].upper() in all_prefixes:
                options.insert(0, prop_val)
            elif prop_val.upper() in all_prefixes:
                options.insert(0, f"{prop_val.upper()}{num_str}{suffix_str}")
        for pfx in all_prefixes:
            concrete = f"{pfx}{num_str}{suffix_str}"
            if concrete not in options:
                options.append(concrete)
        for pfx in all_prefixes:
            if pfx not in options:
                options.append(pfx)
        return options

    # 2. Symbol decisions: valid pin-compatible target standard symbols from Catalog.load_builtin()
    if code == "unmapped_symbol" or dec.kind == "symbol_swap":
        if catalog is None:
            from panelcad.library.catalog import Library

            catalog = Library.load_builtin()
        tgt_prefix = "nema." if tgt == "NEMA/ANSI" else "iec."
        req_pins: set[str] = {str(p) for p in (before.get("wired_pins") or []) if str(p)}
        if not req_pins and project is not None and dec.component_id:
            comp_prefix = f"{dec.component_id}."
            for sh in project.sheets:
                if dec.sheet_id and sh.id != dec.sheet_id:
                    continue
                for w in sh.wires:
                    for end in (str(w.frm or ""), str(w.to or "")):
                        if end.startswith(comp_prefix):
                            req_pins.add(end[len(comp_prefix):])
        if not req_pins and orig_val:
            try:
                src_dev = catalog.get(orig_val)
                req_pins = {str(p.id) for p in src_dev.pins}
            except Exception:  # noqa: BLE001
                pass

        devices_iter = getattr(catalog, "devices", {})
        dev_values = list(devices_iter.values()) if isinstance(devices_iter, dict) else list(devices_iter)
        compatible: list[str] = []
        for dev in dev_values:
            lid = str(getattr(dev, "library_id", "") or "")
            dev_std = normalize_standard(str(getattr(dev, "standard", "") or ""), default="")
            if not (lid.startswith(tgt_prefix) or dev_std == tgt):
                continue
            target_pin_ids = {str(p.id) for p in getattr(dev, "pins", [])}
            if not req_pins:
                compatible.append(lid)
                continue
            bridge = {} if (req_pins <= target_pin_ids) else _default_pin_bridge(req_pins, target_pin_ids, tgt)
            missing = [p for p in req_pins if bridge.get(p, p) not in target_pin_ids]
            mapped_targets = [bridge.get(p, p) for p in req_pins]
            if not missing and len(set(mapped_targets)) == len(mapped_targets):
                compatible.append(lid)

        compatible.sort()
        if prop_val and prop_val in compatible:
            compatible.remove(prop_val)
            compatible.insert(0, prop_val)
        return compatible

    # 3. Note decisions: reworded proposal vs original text
    if code == "note_reworded" or dec.kind == "note_reword":
        opts: list[str] = []
        for candidate in (prop_val, orig_val):
            if candidate and candidate not in opts:
                opts.append(candidate)
        return opts or [orig_val]

    # 4. Layout candidate decisions
    if code == "layout_candidate":
        opts = []
        cands = before.get("candidates")
        if isinstance(cands, list):
            for c in cands:
                cid = str(c.get("id") or c.get("candidate_id") or "") if isinstance(c, dict) else str(c)
                if cid and cid not in opts:
                    opts.append(cid)
        for fallback_cid in (prop_val, orig_val, "ladder_standard", "ladder_compact"):
            if fallback_cid and fallback_cid not in opts:
                opts.append(fallback_cid)
        return opts

    # 5. Other decisions
    opts = []
    for candidate in (prop_val, orig_val):
        if candidate and candidate not in opts:
            opts.append(candidate)
    return opts or [ str(dec.component_id or dec.reason_code) ]



def _apply_tag_rename_on_project(
    project: Project,
    *,
    orig_tag: str,
    current_tag: str,
    desired_tag: str,
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    swapped: list[dict[str, Any]],
) -> None:
    """Rename a device family from ``current_tag`` to ``desired_tag`` across sheets, wires, and maps."""

    if not current_tag or not desired_tag:
        return

    parsed_orig = parse_device_tag(orig_tag) if orig_tag else None
    orig_base = f"{parsed_orig[0]}{parsed_orig[1]}" if (parsed_orig and parsed_orig[1] is not None) else orig_tag
    parsed_des = parse_device_tag(desired_tag)
    des_base = f"{parsed_des[0]}{parsed_des[1]}" if (parsed_des and parsed_des[1] is not None) else desired_tag

    # Build a mapping of (old_current_id -> new_desired_id) and (orig_id -> new_desired_id)
    curr_to_new: dict[str, str] = {current_tag: desired_tag}
    if orig_tag:
        tag_map[orig_tag] = desired_tag

    # Also update any child contacts (e.g. K1-14 -> CR1-14 or M1-14 -> CR1-14)
    for old_k, cur_v in list(tag_map.items()):
        if old_k == orig_tag:
            continue
        if orig_base and old_k.startswith(f"{orig_base}-"):
            suffix = old_k[len(orig_base):]
            child_new = f"{des_base}{suffix}"
            curr_to_new[cur_v] = child_new
            curr_to_new[old_k] = child_new
            tag_map[old_k] = child_new

    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in curr_to_new:
                new_cid = curr_to_new[comp.id]
                if getattr(comp, "label", None) == comp.id:
                    comp.label = new_cid
                comp.id = new_cid
            parent_ref = str(comp.properties.get("parent") or "")
            if parent_ref in curr_to_new:
                comp.properties["parent"] = curr_to_new[parent_ref]
            elif orig_base and parent_ref == orig_base:
                comp.properties["parent"] = des_base

        for wire in sheet.wires:
            for attr in ("frm", "to"):
                end = str(getattr(wire, attr, "") or "")
                if "." in end:
                    ref, pin = end.split(".", 1)
                    if ref in curr_to_new:
                        setattr(wire, attr, f"{curr_to_new[ref]}.{pin}")
                elif end in curr_to_new:
                    setattr(wire, attr, curr_to_new[end])

    for row in pin_rewrites:
        ref = str(row.get("ref") or "")
        if ref in curr_to_new:
            row["ref"] = curr_to_new[ref]

    for row in swapped:
        rid = str(row.get("id") or "")
        if rid in curr_to_new:
            row["id"] = curr_to_new[rid]


def _apply_symbol_change_on_project(
    project: Project,
    *,
    sheet_id: str | None,
    comp_id: str,
    orig_lib: str,
    desired_lib: str,
    target_std: str,
    catalog: Any,
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    swapped: list[dict[str, Any]],
) -> dict[str, str]:
    """Swap ``comp.library_id`` to ``desired_lib`` and update wire pin endpoints and ``pin_rewrites``."""

    from panelcad.convert.ai_assist import _default_pin_bridge

    target_comp = None
    target_sheet = None
    candidate_ids = [comp_id]
    if comp_id in tag_map and tag_map[comp_id] not in candidate_ids:
        candidate_ids.append(tag_map[comp_id])
    for k, v in tag_map.items():
        if v == comp_id and k not in candidate_ids:
            candidate_ids.append(k)

    for sh in project.sheets:
        if sheet_id and sh.id != sheet_id:
            continue
        for c in sh.components:
            if c.id in candidate_ids:
                target_comp = c
                target_sheet = sh
                break
        if target_comp is not None:
            break

    if target_comp is None or target_sheet is None:
        return {}

    actual_id = target_comp.id
    # First undo any prior pin rewrites for actual_id on target_sheet so we start from source pins
    prior_rewrites = [r for r in pin_rewrites if str(r.get("ref") or "") == actual_id and str(r.get("sheet") or target_sheet.id) == target_sheet.id]
    if prior_rewrites:
        pin_back = {str(r["pin"]): str(r["was"]) for r in prior_rewrites if r.get("pin") and r.get("was")}
        for w in target_sheet.wires:
            for attr in ("frm", "to"):
                end = str(getattr(w, attr, "") or "")
                if end.startswith(f"{actual_id}."):
                    p = end.split(".", 1)[1]
                    if p in pin_back:
                        setattr(w, attr, f"{actual_id}.{pin_back[p]}")
        pin_rewrites[:] = [
            r for r in pin_rewrites
            if not (str(r.get("ref") or "") == actual_id and str(r.get("sheet") or target_sheet.id) == target_sheet.id)
        ]

    # Collect current wired pins on target_sheet for actual_id
    wired_pins: set[str] = set()
    for w in target_sheet.wires:
        for attr in ("frm", "to"):
            end = str(getattr(w, attr, "") or "")
            if end.startswith(f"{actual_id}."):
                wired_pins.add(end.split(".", 1)[1])

    applied_pin_map: dict[str, str] = {}
    if desired_lib != orig_lib:
        try:
            target_dev = catalog.get(desired_lib)
            target_pin_ids = {str(p.id) for p in target_dev.pins}
            bridge = {} if (wired_pins <= target_pin_ids) else _default_pin_bridge(wired_pins, target_pin_ids, target_std)
            for w in target_sheet.wires:
                for attr in ("frm", "to"):
                    end = str(getattr(w, attr, "") or "")
                    if end.startswith(f"{actual_id}."):
                        old_p = end.split(".", 1)[1]
                        new_p = bridge.get(old_p, old_p)
                        if new_p != old_p:
                            setattr(w, attr, f"{actual_id}.{new_p}")
                            applied_pin_map[old_p] = new_p
            for old_p, new_p in sorted(applied_pin_map.items()):
                pin_rewrites.append({
                    "sheet": target_sheet.id,
                    "ref": actual_id,
                    "was": old_p,
                    "pin": new_p,
                })
        except Exception:  # noqa: BLE001
            pass

    target_comp.library_id = desired_lib
    swapped[:] = [
        r for r in swapped
        if not (str(r.get("id") or "") == actual_id and str(r.get("sheet") or target_sheet.id) == target_sheet.id)
    ]
    if orig_lib and desired_lib != orig_lib:
        swapped.append({
            "sheet": target_sheet.id,
            "id": actual_id,
            "from": orig_lib,
            "to": desired_lib,
        })
    return applied_pin_map


def _apply_note_change_on_project(
    project: Project,
    *,
    sheet_id: str | None,
    comp_id: str | None,
    field_name: str,
    orig_text: str,
    desired_text: str,
) -> None:
    """Update a reworded note, title, or component callout on ``project``."""

    if field_name.startswith("project.properties."):
        parts = field_name.split(".")
        key = parts[2]
        if len(parts) == 3:
            if isinstance(project.properties.get(key), str):
                project.properties[key] = desired_text
                return
        elif len(parts) == 4 and parts[3].startswith("[") and parts[3].endswith("]"):
            idx = int(parts[3][1:-1])
            lst = project.properties.get(key)
            if isinstance(lst, list) and 0 <= idx < len(lst):
                if isinstance(lst[idx], dict) and "text" in lst[idx]:
                    lst[idx]["text"] = desired_text
                else:
                    lst[idx] = desired_text
                return

    proj_notes = project.properties.get("notes")
    if isinstance(proj_notes, list):
        for idx, item in enumerate(proj_notes):
            if isinstance(item, str) and (item == orig_text or f"[{idx}]" in field_name):
                proj_notes[idx] = desired_text
                return
            if isinstance(item, dict) and (item.get("text") == orig_text or f"[{idx}]" in field_name):
                item["text"] = desired_text
                return

    for sh in project.sheets:
        if sheet_id and sh.id != sheet_id:
            continue
        if comp_id:
            for c in sh.components:
                if c.id == comp_id:
                    if field_name in {"label", "component.label"} or getattr(c, "label", None) in {orig_text, desired_text}:
                        c.label = desired_text
                        return
                    for k in ("description", "note", "notes", "callout", "text", "title", "comment", "function_text"):
                        if field_name.endswith(k) or c.properties.get(k) in {orig_text, desired_text}:
                            c.properties[k] = desired_text
                            return
        if field_name in {"title", "sheet.title", "name", "sheet.name"} or getattr(sh, "title", None) == orig_text:
            sh.title = desired_text
            return
        if field_name in {"description", "sheet.description", "note", "notes"} or getattr(sh, "description", None) == orig_text:
            sh.description = desired_text
            return


def resolve_conversion_decision(
    target_obj: Any,
    decision_or_action: Any = "accept",
    resolution: str | None = None,
    chosen_option: str | None = None,
    *,
    option: str | None = None,
    value: str | None = None,
    session: Any | None = None,
    project: Project | None = None,
    workspace: Any | None = None,
    out: Any | None = None,
    target: str | None = None,
    catalog: Any | None = None,
    source_project: Project | None = None,
) -> dict[str, Any]:
    """Resolve one conversion decision with ``Accept``, ``Change``, or ``Keep original``.

    - ``Accept``: marks ``accepted=True, needs_review=False``, applies the proposal if not
      already applied, and removes the decision from unresolved ``last_conversion_decisions``.
    - ``Change``: validates ``chosen_option`` against ``valid_decision_options(...)`` /
      ``ROLE_PREFIX_TABLE`` / ``Catalog.load_builtin()``, applies the chosen tag/symbol/text
      update to the working project, marks ``accepted=True, needs_review=False``, and updates
      ``last_conversion_decisions``.
    - ``Keep original``: reverts/keeps the original tag/symbol/text for that item, marks
      ``accepted=False, needs_review=False``, and removes it from unresolved ``last_conversion_decisions``.
    """

    import json
    from pathlib import Path
    from panelcad.convert.from_to_check import check_connections
    from panelcad.convert.netlist import same_netlist, sheet_edges
    from panelcad.convert.table import ROLE_PREFIX_TABLE, parse_device_tag, target_standard

    # Disambiguate calling signature
    is_direct_decision = isinstance(target_obj, ConversionDecision) or (
        isinstance(target_obj, dict)
        and ("reason_code" in target_obj or "flag" in target_obj)
        and "chunks" not in target_obj
        and "source_project" not in target_obj
    )
    if is_direct_decision:
        dec_selector: Any = target_obj
        res_raw = str(decision_or_action if decision_or_action is not None else (resolution or "accept"))
        picked = chosen_option if chosen_option is not None else resolution
    else:
        dec_selector = decision_or_action
        res_raw = str(resolution or "accept")
        picked = chosen_option

    if picked is None:
        picked = option if option is not None else value

    res_norm = res_raw.strip().lower().replace("-", "_").replace(" ", "_")
    if res_norm in {"accept", "accepted"}:
        res_key = "accept"
    elif res_norm in {"change", "changed", "modify", "override"}:
        res_key = "change"
    elif res_norm in {"keep_original", "keep", "revert", "original", "reject"}:
        res_key = "keep_original"
    else:
        raise ValueError(f"Unknown decision resolution action {res_raw!r}; expected Accept, Change, or Keep original")

    ws_dict: dict[str, Any] | None = None
    ws_file: Path | None = None

    # Unwrap target_obj
    if not is_direct_decision:
        if hasattr(target_obj, "session") and hasattr(target_obj, "workspace"):
            # ConversionEngine
            session = session or target_obj.session
            project = project or target_obj.session.project
            catalog = catalog or target_obj.session.catalog
            if workspace is None and getattr(target_obj, "workspace", None) is not None:
                workspace = target_obj.workspace
            if out is None and getattr(target_obj, "out", None) is not None:
                out = target_obj.out
            if target is None and getattr(target_obj, "target", None) is not None:
                target = target_obj.target
        elif hasattr(target_obj, "project") and hasattr(target_obj, "catalog"):
            session = session or target_obj
            project = project or target_obj.project
            catalog = catalog or target_obj.catalog
        elif isinstance(target_obj, Project):
            project = project or target_obj
        elif isinstance(target_obj, dict):
            ws_dict = target_obj
        elif isinstance(target_obj, (str, Path)):
            workspace = target_obj

    if project is None and session is not None:
        project = getattr(session, "project", None)
    if catalog is None and session is not None:
        catalog = getattr(session, "catalog", None)
    if catalog is None:
        from panelcad.library.catalog import Library

        catalog = Library.load_builtin()

    if workspace is None and session is not None:
        workspace = getattr(session, "_conversion_workspace", None) or getattr(session, "_convert_workspace", None)
    if workspace is None and project is not None:
        workspace = getattr(project, "_conversion_workspace", None) or getattr(project, "_convert_workspace", None)

    if isinstance(workspace, dict):
        ws_dict = workspace
    elif isinstance(workspace, (str, Path)):
        p = Path(workspace)
        if p.is_dir() and (p / "workspace.json").exists():
            ws_file = p / "workspace.json"
            ws_dict = json.loads(ws_file.read_text(encoding="utf-8"))
        elif p.is_file():
            ws_file = p
            ws_dict = json.loads(p.read_text(encoding="utf-8"))

    if ws_dict is None and session is not None:
        cand_ws = getattr(session, "_conversion_workspace", None)
        if isinstance(cand_ws, dict):
            ws_dict = cand_ws
    if ws_dict is None and project is not None:
        cand_ws = getattr(project, "_conversion_workspace", None)
        if isinstance(cand_ws, dict):
            ws_dict = cand_ws
    if ws_file is None and session is not None:
        cand_f = getattr(session, "_convert_workspace", None)
        if isinstance(cand_f, (str, Path)):
            ws_file = Path(cand_f)

    if project is None and ws_dict is not None and isinstance(ws_dict.get("working_project"), dict):
        project = Project.model_validate(ws_dict["working_project"])

    conv_block: dict[str, Any] = {}
    if project is not None and isinstance(project.properties.get("convert_standard"), dict):
        conv_block = project.properties["convert_standard"]

    target_std = target_standard(
        target
        or (ws_dict.get("target") if ws_dict else None)
        or conv_block.get("to")
        or (project.properties.get("standard") if project else None)
        or "NEMA/ANSI"
    )

    # Locate the decisions list and the target decision
    raw_decisions: list[dict[str, Any]] = []
    if ws_dict is not None and isinstance(ws_dict.get("decisions"), list) and ws_dict["decisions"]:
        raw_decisions = [ConversionDecision.from_dict(d).to_dict() for d in ws_dict["decisions"]]
    elif ws_dict is not None and isinstance(ws_dict.get("chunks"), list):
        for ch in ws_dict["chunks"]:
            for d in ch.get("decisions") or []:
                raw_decisions.append(ConversionDecision.from_dict(d).to_dict())
    elif isinstance(conv_block.get("decisions"), list):
        raw_decisions = [ConversionDecision.from_dict(d).to_dict() for d in conv_block["decisions"]]
    elif project is not None and isinstance(project.properties.get("last_conversion_decisions"), list):
        raw_decisions = [ConversionDecision.from_dict(d).to_dict() for d in project.properties["last_conversion_decisions"]]

    target_idx: int | None = None
    target_dec: ConversionDecision | None = None

    if isinstance(dec_selector, int):
        if 0 <= dec_selector < len(raw_decisions):
            target_idx = dec_selector
            target_dec = ConversionDecision.from_dict(raw_decisions[dec_selector])
        elif project is not None and isinstance(project.properties.get("last_conversion_decisions"), list):
            last_decs = project.properties["last_conversion_decisions"]
            if 0 <= dec_selector < len(last_decs):
                cand = ConversionDecision.from_dict(last_decs[dec_selector])
                for i, d in enumerate(raw_decisions):
                    if (
                        str(d.get("reason_code") or "") == cand.reason_code
                        and str(d.get("component_id") or "") == str(cand.component_id or "")
                        and str(d.get("sheet_id") or "") == str(cand.sheet_id or "")
                    ):
                        target_idx = i
                        target_dec = ConversionDecision.from_dict(d)
                        break
                if target_dec is None:
                    target_dec = cand
                    raw_decisions.append(cand.to_dict())
                    target_idx = len(raw_decisions) - 1
    elif isinstance(dec_selector, (ConversionDecision, dict)):
        cand = ConversionDecision.from_dict(dec_selector)
        for i, d in enumerate(raw_decisions):
            if (
                str(d.get("reason_code") or "") == cand.reason_code
                and str(d.get("component_id") or "") == str(cand.component_id or "")
                and str(d.get("sheet_id") or "") == str(cand.sheet_id or "")
            ):
                target_idx = i
                target_dec = ConversionDecision.from_dict(d)
                break
        if target_dec is None:
            for i, d in enumerate(raw_decisions):
                if (
                    str(d.get("reason_code") or "") == cand.reason_code
                    and (
                        str(d.get("component_id") or "") == str(cand.component_id or "")
                        or decision_original_value(d) == decision_original_value(cand)
                    )
                ):
                    target_idx = i
                    target_dec = ConversionDecision.from_dict(d)
                    break
        if target_dec is None:
            target_dec = cand
            raw_decisions.append(cand.to_dict())
            target_idx = len(raw_decisions) - 1
    elif isinstance(dec_selector, str):
        sel = dec_selector.strip()
        for i, d in enumerate(raw_decisions):
            dec_i = ConversionDecision.from_dict(d)
            if (
                str(dec_i.component_id or "") == sel
                or decision_original_value(dec_i) == sel
                or decision_proposal_value(dec_i) == sel
                or dec_i.reason_code == sel
            ):
                target_idx = i
                target_dec = dec_i
                break

    if target_dec is None:
        raise ValueError(f"Could not find conversion decision matching {dec_selector!r}")

    # Prepare mutable maps from workspace / project
    tag_map: dict[str, str] = {}
    if ws_dict is not None and isinstance(ws_dict.get("tag_map"), dict) and ws_dict["tag_map"]:
        tag_map = {str(k): str(v) for k, v in ws_dict["tag_map"].items()}
    elif ws_dict is not None and isinstance(ws_dict.get("chunks"), list):
        for ch in ws_dict["chunks"]:
            if isinstance(ch.get("tag_map"), dict):
                tag_map.update({str(k): str(v) for k, v in ch["tag_map"].items()})
    elif isinstance(conv_block.get("tags"), dict):
        tag_map = {str(k): str(v) for k, v in conv_block["tags"].items()}

    pin_rewrites: list[dict[str, str]] = []
    if ws_dict is not None:
        for ch in ws_dict.get("chunks") or []:
            if ch.get("kind") == "symbols":
                for r in ch.get("pin_rewrites") or []:
                    pin_rewrites.append(dict(r))
        if not pin_rewrites and isinstance(ws_dict.get("pin_rewrites"), list):
            pin_rewrites = [dict(r) for r in ws_dict["pin_rewrites"]]

    swapped: list[dict[str, Any]] = []
    if ws_dict is not None:
        for ch in ws_dict.get("chunks") or []:
            if ch.get("kind") == "symbols":
                for r in ch.get("swapped") or []:
                    swapped.append(dict(r))
    elif isinstance(conv_block.get("swapped"), list):
        swapped = [dict(r) for r in conv_block["swapped"]]

    code = str(target_dec.reason_code or "")
    orig_val = decision_original_value(target_dec)
    prop_val = decision_proposal_value(target_dec)
    valid_opts = valid_decision_options(target_dec, target=target_std, project=project, catalog=catalog)
    applied_value = prop_val

    # 1. Role / tag decision resolution
    if code in {"ambiguous_role", "tag_collision", "multifunction_block"} or target_dec.kind == "tag_rename":
        orig_tag = str(target_dec.before.get("id") or target_dec.before.get("tag") or orig_val or target_dec.component_id or "")
        current_tag = str(tag_map.get(orig_tag) or target_dec.after.get("id") or target_dec.after.get("tag") or target_dec.component_id or orig_tag)
        if project is not None:
            all_comp_ids = {c.id for sh in project.sheets for c in sh.components}
            if current_tag not in all_comp_ids:
                for fallback_id in (str(target_dec.component_id or ""), str(target_dec.after.get("id") or ""), orig_tag):
                    if fallback_id and fallback_id in all_comp_ids:
                        current_tag = fallback_id
                        break

        parsed_orig = parse_device_tag(orig_tag) if orig_tag else None
        num_str = str(parsed_orig[1]) if (parsed_orig and parsed_orig[1] is not None) else "1"
        suffix_str = str(parsed_orig[2] or "") if parsed_orig else ""

        if res_key == "accept":
            prop_is_valid = bool(target_dec.proposal.get("valid", True)) if isinstance(target_dec.proposal, dict) else True
            cand_id = str(target_dec.proposal.get("id") or "") if isinstance(target_dec.proposal, dict) else ""
            cand_pfx = str(target_dec.proposal.get("prefix") or "") if isinstance(target_dec.proposal, dict) else ""
            if prop_is_valid and cand_id:
                desired_tag = cand_id
            elif prop_is_valid and cand_pfx:
                desired_tag = f"{cand_pfx}{num_str}{suffix_str}"
            else:
                desired_tag = current_tag or orig_tag
            applied_value = desired_tag
            target_dec.accepted = True
            target_dec.needs_review = False
        elif res_key == "change":
            if not picked or not str(picked).strip():
                raise ValueError("Change resolution requires a non-empty chosen_option")
            raw_pick = str(picked).strip()
            prefix_key = "nema_prefix" if target_std == "NEMA/ANSI" else "iec_prefix"
            valid_prefixes = {str(r.get(prefix_key) or "").upper(): str(r.get(prefix_key) or "") for r in ROLE_PREFIX_TABLE if r.get(prefix_key)}
            role_to_prefix = {str(r.get("role") or "").lower(): str(r.get(prefix_key) or "") for r in ROLE_PREFIX_TABLE if r.get("role") and r.get(prefix_key)}
            for r in ROLE_PREFIX_TABLE:
                for tok in r.get("role_tokens", ()):
                    if r.get(prefix_key):
                        role_to_prefix[str(tok).lower()] = str(r.get(prefix_key))

            desired_tag = ""
            if raw_pick in valid_opts:
                if raw_pick.upper() in valid_prefixes and not any(ch.isdigit() for ch in raw_pick):
                    desired_tag = f"{valid_prefixes[raw_pick.upper()]}{num_str}{suffix_str}"
                else:
                    desired_tag = raw_pick
            elif raw_pick.upper() in valid_prefixes:
                desired_tag = f"{valid_prefixes[raw_pick.upper()]}{num_str}{suffix_str}"
            elif raw_pick.lower() in role_to_prefix:
                desired_tag = f"{role_to_prefix[raw_pick.lower()]}{num_str}{suffix_str}"
            else:
                parsed_pick = parse_device_tag(raw_pick)
                if parsed_pick and parsed_pick[0].upper() in valid_prefixes and parsed_pick[1] is not None:
                    desired_tag = f"{valid_prefixes[parsed_pick[0].upper()]}{parsed_pick[1]}{parsed_pick[2] or ''}"
                else:
                    raise ValueError(
                        f"Invalid option {raw_pick!r} for role/tag for {target_dec.component_id}; "
                        f"valid options: {valid_opts}"
                    )

            # Avoid colliding with another unrelated device on the project if user picked a bare prefix
            if project is not None and not any(ch.isdigit() for ch in raw_pick):
                existing_ids = {c.id for sh in project.sheets for c in sh.components} - {current_tag, orig_tag}
                pfx_only = valid_prefixes.get(raw_pick.upper()) or role_to_prefix.get(raw_pick.lower()) or ""
                if pfx_only and desired_tag in existing_ids:
                    n = 1
                    while f"{pfx_only}{n}{suffix_str}" in existing_ids:
                        n += 1
                    desired_tag = f"{pfx_only}{n}{suffix_str}"

            applied_value = desired_tag
            target_dec.accepted = True
            target_dec.needs_review = False
        else:  # keep_original
            desired_tag = orig_tag or current_tag
            applied_value = desired_tag
            target_dec.accepted = False
            target_dec.needs_review = False

        if project is not None and desired_tag and current_tag:
            _apply_tag_rename_on_project(
                project,
                orig_tag=orig_tag,
                current_tag=current_tag,
                desired_tag=desired_tag,
                tag_map=tag_map,
                pin_rewrites=pin_rewrites,
                swapped=swapped,
            )
        target_dec.component_id = desired_tag
        target_dec.after["id"] = desired_tag
        if "tag" in target_dec.after or "tag" in target_dec.before:
            target_dec.after["tag"] = desired_tag
        if not isinstance(target_dec.proposal, dict):
            target_dec.proposal = {}
        target_dec.proposal["id"] = desired_tag
        target_dec.proposal["chosen_option"] = applied_value

    # 2. Symbol decision resolution
    elif code == "unmapped_symbol" or target_dec.kind == "symbol_swap":
        orig_lib = str(target_dec.before.get("library_id") or orig_val or "")
        comp_id = str(target_dec.component_id or "")
        if res_key == "accept":
            prop_valid = bool(target_dec.proposal.get("valid", True)) if isinstance(target_dec.proposal, dict) else True
            cand_lib = str(target_dec.proposal.get("library_id") or target_dec.after.get("library_id") or orig_lib)
            if prop_valid and cand_lib in valid_opts:
                desired_lib = cand_lib
            elif cand_lib == orig_lib:
                desired_lib = orig_lib
            else:
                raise ValueError(f"Cannot accept invalid symbol proposal {cand_lib!r} for {comp_id}")
            applied_value = desired_lib
            target_dec.accepted = True
            target_dec.needs_review = False
        elif res_key == "change":
            if not picked or not str(picked).strip():
                raise ValueError("Change resolution requires a non-empty chosen_option")
            raw_pick = str(picked).strip()
            if raw_pick not in valid_opts:
                raise ValueError(
                    f"Invalid or pin-incompatible symbol option {raw_pick!r} for {comp_id}; "
                    f"valid options: {valid_opts}"
                )
            desired_lib = raw_pick
            applied_value = desired_lib
            target_dec.accepted = True
            target_dec.needs_review = False
        else:  # keep_original
            desired_lib = orig_lib
            applied_value = desired_lib
            target_dec.accepted = False
            target_dec.needs_review = False

        applied_pins: dict[str, str] = {}
        if project is not None and comp_id and desired_lib:
            applied_pins = _apply_symbol_change_on_project(
                project,
                sheet_id=target_dec.sheet_id,
                comp_id=comp_id,
                orig_lib=orig_lib,
                desired_lib=desired_lib,
                target_std=target_std,
                catalog=catalog,
                tag_map=tag_map,
                pin_rewrites=pin_rewrites,
                swapped=swapped,
            )
        target_dec.after["library_id"] = desired_lib
        target_dec.after["pin_map"] = applied_pins
        if not isinstance(target_dec.proposal, dict):
            target_dec.proposal = {}
        target_dec.proposal["library_id"] = desired_lib
        target_dec.proposal["chosen_option"] = applied_value

    # 3. Note / text decision resolution
    elif code in {"note_reworded", "standard_specific_note"} or target_dec.kind == "note_reword":
        orig_text = str(target_dec.before.get("original") or target_dec.before.get("original_text") or target_dec.before.get("text") or orig_val or "")
        prop_text = str(
            (target_dec.proposal.get("text") if isinstance(target_dec.proposal, dict) else "")
            or (target_dec.proposal.get("reworded_text") if isinstance(target_dec.proposal, dict) else "")
            or (target_dec.proposal.get("proposal") if isinstance(target_dec.proposal, dict) else "")
            or target_dec.after.get("text")
            or orig_text
        )
        field_name = str(
            target_dec.before.get("field")
            or (target_dec.proposal.get("field") if isinstance(target_dec.proposal, dict) else "")
            or "note"
        )
        if res_key == "accept":
            desired_text = prop_text
            applied_value = desired_text
            target_dec.accepted = True
            target_dec.needs_review = False
        elif res_key == "change":
            if picked is None or not str(picked).strip():
                raise ValueError("Change resolution requires a non-empty chosen_option")
            desired_text = str(picked)
            applied_value = desired_text
            target_dec.accepted = True
            target_dec.needs_review = False
        else:  # keep_original
            desired_text = orig_text
            applied_value = desired_text
            target_dec.accepted = False
            target_dec.needs_review = False

        if project is not None:
            _apply_note_change_on_project(
                project,
                sheet_id=target_dec.sheet_id,
                comp_id=target_dec.component_id,
                field_name=field_name,
                orig_text=orig_text if res_key != "keep_original" else prop_text,
                desired_text=desired_text,
            )
        target_dec.after["text"] = desired_text
        if not isinstance(target_dec.proposal, dict):
            target_dec.proposal = {}
        target_dec.proposal["text"] = desired_text
        target_dec.proposal["proposal"] = desired_text
        target_dec.proposal["chosen_option"] = applied_value

    # 4. Layout candidate or other decision resolution
    else:
        if res_key == "accept":
            applied_value = prop_val or orig_val
            target_dec.accepted = True
            target_dec.needs_review = False
        elif res_key == "change":
            if picked is None or not str(picked).strip():
                raise ValueError("Change resolution requires a non-empty chosen_option")
            raw_pick = str(picked).strip()
            if valid_opts and raw_pick not in valid_opts:
                raise ValueError(f"Invalid option {raw_pick!r} for {code}; valid options: {valid_opts}")
            applied_value = raw_pick
            target_dec.accepted = True
            target_dec.needs_review = False
        else:
            applied_value = orig_val
            target_dec.accepted = False
            target_dec.needs_review = False
        if not isinstance(target_dec.proposal, dict):
            target_dec.proposal = {}
        target_dec.proposal["chosen_option"] = applied_value

    target_dec.proposal["resolved"] = True
    target_dec.proposal["resolution"] = res_key
    updated_dec_dict = target_dec.to_dict()
    updated_dec_dict["accepted"] = target_dec.accepted
    updated_dec_dict["resolution"] = res_key
    updated_dec_dict["chosen_option"] = applied_value

    if target_idx is not None and 0 <= target_idx < len(raw_decisions):
        raw_decisions[target_idx] = updated_dec_dict
    else:
        raw_decisions.append(updated_dec_dict)

    # Refresh cross-references if session is available
    if session is not None:
        try:
            from panelcad.core.xref import refresh_xrefs

            refresh_xrefs(session)
        except Exception:  # noqa: BLE001
            pass

    # Update workspace chunks & top-level structures if present
    rail_map: dict[str, dict[str, Any]] = {}
    if ws_dict is not None:
        ws_dict["tag_map"] = dict(tag_map)
        ws_dict["decisions"] = list(raw_decisions)
        for ch in ws_dict.get("chunks") or []:
            if ch.get("kind") == "tags":
                ch["tag_map"] = dict(tag_map)
            elif ch.get("kind") == "symbols":
                sh_id = str(ch.get("sheet") or "")
                ch["pin_rewrites"] = [r for r in pin_rewrites if str(r.get("sheet") or sh_id) == sh_id]
                ch["swapped"] = [r for r in swapped if str(r.get("sheet") or sh_id) == sh_id]
            if isinstance(ch.get("rail"), dict):
                sh_id = str(ch.get("sheet") or "")
                prev_r = dict(rail_map.get(sh_id) or {})
                prev_wn = dict(prev_r.get("wire_numbers") or {})
                prev_r.update(ch["rail"])
                if prev_wn or ch["rail"].get("wire_numbers"):
                    prev_wn.update(ch["rail"].get("wire_numbers") or {})
                    prev_r["wire_numbers"] = prev_wn
                rail_map[sh_id] = prev_r
            # Update matching decision inside chunk["decisions"] and chunk["ai_decisions"]
            for key_name in ("decisions", "ai_decisions"):
                ch_decs = ch.get(key_name)
                if isinstance(ch_decs, list):
                    for idx_c, cd in enumerate(ch_decs):
                        cd_obj = ConversionDecision.from_dict(cd)
                        if (
                            cd_obj.reason_code == target_dec.reason_code
                            and (
                                cd_obj.component_id == target_dec.component_id
                                or decision_original_value(cd_obj) == orig_val
                            )
                            and str(cd_obj.sheet_id or "") == str(target_dec.sheet_id or "")
                        ):
                            ch_decs[idx_c] = updated_dec_dict

    if not rail_map and isinstance(conv_block.get("rails"), dict):
        rail_map = dict(conv_block["rails"])

    # Update project metadata & last_conversion_decisions
    remaining_unresolved = [
        d for d in raw_decisions
        if bool(d.get("needs_review", False))
    ]
    if project is not None:
        if remaining_unresolved:
            project.properties["last_conversion_decisions"] = list(remaining_unresolved)
        else:
            project.properties.pop("last_conversion_decisions", None)
        if isinstance(project.properties.get("convert_standard"), dict):
            project.properties["convert_standard"]["tags"] = dict(tag_map)
            project.properties["convert_standard"]["swapped"] = list(swapped)
            project.properties["convert_standard"]["decisions"] = list(raw_decisions)
            project.properties["convert_standard"]["unresolved_decisions"] = list(remaining_unresolved)

    # Re-run netlist and From-To connection gates
    netlist_ok = True
    conn_check: dict[str, Any] = {"ok": True}
    src_proj = source_project
    if src_proj is None and ws_dict is not None and isinstance(ws_dict.get("source_project"), dict):
        src_proj = Project.model_validate(ws_dict["source_project"])
    if project is not None and src_proj is not None:
        before_edges = [tuple(r) for r in (ws_dict.get("before_edges") or sheet_edges(src_proj))]
        after_edges = sheet_edges(project)
        netlist_ok = same_netlist(
            before_edges,
            after_edges,
            tag_map=tag_map,
            pin_rewrites=pin_rewrites,
            rail_map=rail_map,
        )
        conn_check = check_connections(
            src_proj,
            project,
            catalog,
            tag_map=tag_map,
            pin_rewrites=pin_rewrites,
            rail_map=rail_map,
        )
        if ws_dict is not None:
            ws_dict["working_project"] = project.model_dump(mode="json")
            ws_dict["connection_check"] = conn_check

    if ws_file is not None and ws_dict is not None:
        ws_file.parent.mkdir(parents=True, exist_ok=True)
        ws_file.write_text(json.dumps(ws_dict, indent=2), encoding="utf-8")

    save_path = out
    if save_path is None and ws_dict is not None and ws_dict.get("out_written") and ws_dict.get("out_path"):
        cand_out = str(ws_dict.get("out_path") or "")
        cand_src = str(ws_dict.get("source_path") or "")
        if cand_out and cand_out != cand_src:
            save_path = cand_out
    if save_path and project is not None:
        sp = Path(save_path)
        if session is not None and hasattr(session, "save"):
            session.save(sp)
        else:
            from panelcad.api.session import Session as _Session

            _Session(project, path=sp).save(sp)

    return {
        "decision": updated_dec_dict,
        "accepted": target_dec.accepted,
        "needs_review": target_dec.needs_review,
        "resolution": res_key,
        "chosen_option": applied_value,
        "same_netlist": netlist_ok,
        "connection_check": conn_check,
        "tag_map": dict(tag_map),
        "decisions": list(raw_decisions),
        "unresolved_decisions": list(remaining_unresolved),
        "unresolved_count": len(remaining_unresolved),
    }
