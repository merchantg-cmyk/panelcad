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
    def accepted(self) -> bool:
        if isinstance(self.proposal, dict) and "accepted" in self.proposal:
            return bool(self.proposal["accepted"])
        return not self.needs_review

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
            proposal=dict(data.get("proposal") or {}),
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
            if status != "accepted" and (dec.needs_review or bool(dec.model)):
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
