"""Advisory AI helper for IEC <-> NEMA conversion.

AI assistance in PanelCAD conversion is strictly advisory and gated by
deterministic code validation and a confidence threshold:
- Never edits silently: every AI-assisted proposal is recorded as a
  ``ConversionDecision`` with ``model``, ``confidence``, ``proposal``, and
  ``reason``.
- Hard code validation runs before any proposal can take effect:
  1. Ambiguous device role resolution is validated against ``ROLE_PREFIX_TABLE``.
  2. Unmapped symbol suggestions are validated against the target builtin library
     and must cover all wired pins of the component (``wired_pins <= mapped_target_pins``).
  3. Sheet note / callout / title rewording preserves the original text in the
     decision history so ``revert`` restores it cleanly.
  4. Ladder layout candidate ranking disqualifies any candidate with hard
     ``sheet_flags`` before ranking.
- When ``mode="rules_only"`` (``ai_assist=False``, the default), this module is
  not invoked and conversion produces identical output and decisions to rules-only
  conversion.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from panelcad.convert.plan import ConversionDecision
from panelcad.convert.standard_map import ROLE_TAG_TABLE
from panelcad.convert.table import (
    role_entry_for,
    role_prefix_for,
    target_standard,
)

SHARED_IEC_PREFIXES: frozenset[str] = frozenset({"K", "Q", "S", "F", "B"})
SHARED_NEMA_PREFIXES: frozenset[str] = frozenset({"PB", "M", "CR", "CB", "OL"})
from panelcad.convert.table import parse_device_tag
from panelcad.library.catalog import normalize_standard


def split_prefix(raw_tag: str) -> tuple[str, str]:
    parsed = parse_device_tag(raw_tag)
    if parsed is not None:
        return parsed[0], f"{parsed[1]}{parsed[2]}"
    m = re.match(r"^-?([A-Za-z]+)(.*)$", str(raw_tag or "").strip())
    if m:
        return m.group(1), m.group(2)
    return str(raw_tag or "").strip(), ""


DEFAULT_CONFIDENCE_THRESHOLD: float = 0.80
default_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

class _RolePrefixTable(tuple):
    """Tuple of ROLE_TAG_TABLE rows that also supports role-name lookup and membership tests."""

    def _find_entry(self, key: str) -> dict[str, Any] | None:
        token = str(key or "").strip().lower()
        for row in self:
            if str(row.get("role", "")).lower() == token or token in row.get("role_tokens", ()):
                return row
        return None

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return self._find_entry(item) is not None
        return super().__contains__(item)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            entry = self._find_entry(key)
            if entry is None:
                raise KeyError(key)
            return (str(entry["iec_prefix"]), str(entry["nema_prefix"]))
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._find_entry(key)
        if entry is None:
            return default
        return (str(entry["iec_prefix"]), str(entry["nema_prefix"]))


ROLE_PREFIX_TABLE = _RolePrefixTable(ROLE_TAG_TABLE)

HARD_LAYOUT_FLAGS: frozenset[str] = frozenset(
    {
        "component_overlap",
        "rail_collision",
        "wire_overlap",
        "title_block_overlap",
        "layout_overflow",
    }
)

# Deterministic IEC (German / IEC 60204-1 / IEC 81346) -> NEMA ICS / NFPA 79 glossary.
IEC_TO_NEMA_GLOSSARY: dict[str, str] = {
    "Not-Halt": "Emergency Stop",
    "Not-Aus": "Emergency Stop",
    "NOT-HALT": "EMERGENCY STOP",
    "NOT-AUS": "EMERGENCY STOP",
    "Steuerspannung": "Control Voltage",
    "Hauptstromkreis": "Main Power Circuit",
    "Steuerstromkreis": "Control Circuit",
    "Schütz": "Contactor",
    "Leistungsschütz": "Power Contactor",
    "Hilfsschütz": "Control Relay",
    "Steuerrelais": "Control Relay",
    "Motorschutzschalter": "Motor Circuit Protector",
    "Motorschutzrelais": "Overload Relay",
    "Überlastrelais": "Overload Relay",
    "Klemmenleiste": "Terminal Block",
    "Hauptschalter": "Main Disconnect Switch",
    "Lasttrennschalter": "Disconnect Switch",
    "Leitungsschutzschalter": "Miniature Circuit Breaker",
    "Sicherung": "Fuse",
    "Zeitrelais": "Timing Relay",
    "Drucktaster": "Pushbutton",
    "Wahlschalter": "Selector Switch",
    "Endschalter": "Limit Switch",
    "Positionsschalter": "Limit Switch",
    "Meldeleuchte": "Pilot Light",
    "Signalleuchte": "Pilot Light",
    "Frequenzumrichter": "Variable Frequency Drive",
    "Sanftstarter": "Soft Starter",
    "Netzteil": "Power Supply",
    "Stromversorgung": "Power Supply",
    "Steuertransformator": "Control Transformer",
    "Schutzleiter": "Equipment Grounding Conductor (PE)",
    "IEC 60204-1": "NFPA 79",
    "IEC 81346": "NEMA ICS / NFPA 79",
}

NEMA_TO_IEC_GLOSSARY: dict[str, str] = {
    "Emergency Stop": "Not-Halt / Emergency Stop (IEC 60204-1)",
    "Control Voltage": "Steuerspannung",
    "Main Power Circuit": "Hauptstromkreis",
    "Control Circuit": "Steuerstromkreis",
    "Motor Protection Circuit Breaker": "Motorschutzschalter",
    "Overload Relay": "Überlastrelais",
    "Terminal Strip": "Klemmenleiste",
    "Main Disconnect Switch": "Hauptschalter",
    "Timing Relay": "Zeitrelais",
    "NFPA 79": "IEC 60204-1",
}

_NOTE_PROP_KEYS: tuple[str, ...] = (
    "note",
    "notes",
    "callout",
    "description",
    "function_text",
    "label",
    "text",
)


class ConversionAIBackend(Protocol):
    """Pluggable advisory backend for IEC <-> NEMA conversion."""

    name: str
    model: str

    def available(self) -> bool: ...

    def infer(self, task: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class NoOpAIBackend:
    """Rules-only backend. Never calls a model and makes no proposals."""

    name = "noop"
    model = ""

    def available(self) -> bool:
        return False

    def infer(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {}


class RecordedJSONBackend:
    """Deterministic backend backed by recorded JSON responses plus optional glossary/heuristics."""

    name = "recorded_json"

    def __init__(
        self,
        recordings: dict[str, Any] | Path | str | None = None,
        *,
        model: str = "recorded-json/v1",
        use_heuristics: bool = True,
    ) -> None:
        self.model = model
        self.use_heuristics = bool(use_heuristics)
        if isinstance(recordings, (Path, str)):
            path = Path(recordings)
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            self.recordings: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        elif isinstance(recordings, dict):
            self.recordings = json.loads(json.dumps(recordings))
        else:
            self.recordings = {}

    def available(self) -> bool:
        return True

    def _lookup(self, task: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        keys: list[str] = []
        for field_name in ("id", "component_id", "library_id", "text", "sheet_id", "key"):
            val = str(payload.get(field_name) or "").strip()
            if val:
                keys.append(f"{task}:{val}")
                keys.append(val)
        keys.append(task)

        bucket_aliases = {
            "resolve_role": ("resolve_role", "roles", "role"),
            "suggest_symbol": ("suggest_symbol", "symbols", "symbol"),
            "reword_note": ("reword_note", "notes", "note"),
            "rank_layout": ("rank_layout", "rank_layouts", "layouts", "layout"),
        }
        for bname in bucket_aliases.get(task, (task,)):
            task_bucket = self.recordings.get(bname)
            if isinstance(task_bucket, dict):
                for field_name in ("id", "component_id", "library_id", "text", "sheet_id", "key"):
                    val = str(payload.get(field_name) or "").strip()
                    if val and val in task_bucket and isinstance(task_bucket[val], dict):
                        return json.loads(json.dumps(task_bucket[val]))
                if "*" in task_bucket and isinstance(task_bucket["*"], dict):
                    return json.loads(json.dumps(task_bucket["*"]))

        for key in keys:
            hit = self.recordings.get(key)
            if isinstance(hit, dict):
                # Avoid returning a nested bucket dict that is keyed by item id rather than a response
                if key == task and any(
                    isinstance(v, dict) for v in hit.values()
                ) and not any(
                    k in hit
                    for k in ("role", "library_id", "text", "proposal", "selected", "chosen_id", "confidence")
                ):
                    continue
                return json.loads(json.dumps(hit))
        return None

    def infer(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        recorded = self._lookup(task, payload)
        if recorded is not None:
            out = dict(recorded)
            out.setdefault("model", self.model)
            return out
        if not self.use_heuristics:
            return {"confidence": 0.0, "reason": "no recorded response", "model": self.model}
        if task == "resolve_role":
            return _heuristic_role_infer(payload, model=self.model)
        if task == "suggest_symbol":
            return _heuristic_symbol_infer(payload, model=self.model)
        if task == "reword_note":
            return _heuristic_note_infer(payload, model=self.model)
        if task == "rank_layout":
            return _heuristic_layout_infer(payload, model=self.model)
        return {"confidence": 0.0, "reason": f"unknown task {task}", "model": self.model}


FakeConversionAIBackend = RecordedJSONBackend


class ConfiguredConversionAIBackend:
    """Lazy adapter to ``panelcad.ai.config`` (OpenAI-compatible / xAI / Ollama),
    falling back to deterministic offline heuristics when no live endpoint is enabled.
    """

    name = "configured"

    def __init__(self, *, fallback_heuristics: bool = True) -> None:
        self.fallback_heuristics = bool(fallback_heuristics)
        self._fallback = RecordedJSONBackend(use_heuristics=True, model="panelcad-advisor-offline/1")

    @property
    def model(self) -> str:
        try:
            from panelcad.ai.config import configuration_reason, load_ai_lookup

            cfg = load_ai_lookup()
            if not configuration_reason(cfg):
                return cfg.resolved_model() or "configured-ai"
        except Exception:  # noqa: BLE001
            pass
        return self._fallback.model

    def available(self) -> bool:
        try:
            from panelcad.ai.config import configuration_reason, load_ai_lookup

            cfg = load_ai_lookup()
            if not configuration_reason(cfg):
                return True
        except Exception:  # noqa: BLE001
            pass
        return self.fallback_heuristics

    def infer(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from panelcad.ai.config import configuration_reason, load_ai_lookup

            cfg = load_ai_lookup()
            if not configuration_reason(cfg):
                endpoint = cfg.resolved_endpoint()
                model_name = cfg.resolved_model()
                if endpoint and model_name:
                    url = endpoint.rstrip("/") + "/chat/completions"
                    body = json.dumps(
                        {
                            "model": model_name,
                            "temperature": 0.0,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a PanelCAD IEC<->NEMA conversion advisor. "
                                        "Respond only with a JSON object containing confidence (0..1), "
                                        "reason, and task fields."
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": json.dumps({"task": task, "payload": payload}),
                                },
                            ],
                        }
                    ).encode("utf-8")
                    headers = {"Content-Type": "application/json"}
                    if cfg.api_key:
                        headers["Authorization"] = f"Bearer {cfg.api_key}"
                    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=10.0) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                    content = (
                        ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
                    )
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        parsed.setdefault("model", model_name)
                        return parsed
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
            pass
        except Exception:  # noqa: BLE001
            pass
        if self.fallback_heuristics:
            return self._fallback.infer(task, payload)
        return {"confidence": 0.0, "reason": "configured AI endpoint unavailable", "model": self.model}


def resolve_ai_backend(
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    *,
    mode: str = "rules_only",
    ai_assist: bool = False,
) -> ConversionAIBackend:
    """Return the active ``ConversionAIBackend`` for the requested conversion mode."""

    effective_ai = bool(ai_assist) or str(mode or "").strip().lower() in {"ai_assisted", "ai", "assisted"}
    if backend is not None:
        if isinstance(backend, (dict, Path, str)):
            return RecordedJSONBackend(backend)
        return backend
    if not effective_ai:
        return NoOpAIBackend()
    return ConfiguredConversionAIBackend(fallback_heuristics=True)


# ---------------------------------------------------------------------------
# Heuristic / deterministic advisor helpers for RecordedJSONBackend fallback
# ---------------------------------------------------------------------------


def _heuristic_role_infer(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    comp_id = str(payload.get("component_id") or payload.get("id") or "")
    prefix, _ = split_prefix(comp_id)
    prefix = prefix.lstrip("-=").upper()
    lib_id = str(payload.get("library_id") or "").lower()
    pins = {str(p).upper() for p in (payload.get("pins") or [])}
    contacts = [str(c) for c in (payload.get("linked_contacts") or [])]
    wires = [str(w).lower() for w in (payload.get("connected_wires") or [])]
    text_blob = " ".join(
        [
            str(payload.get("nearby_text") or ""),
            str(payload.get("description") or ""),
            str(payload.get("label") or ""),
            str(payload.get("function_text") or ""),
            " ".join(wires),
            " ".join(contacts),
            lib_id,
        ]
    ).lower()

    # Check explicit keyword signals in nearby_text / description / pins / contacts
    if prefix in {"K", "KA", "KT", "KM", "CR", "TR", "M", "AR"} or not prefix:
        if any(tok in text_blob for tok in ("timer", "on-delay", "off-delay", "on_delay", "off_delay", "zeitrelais", "ton", "tof", "delay", "tdr")) or (
            {"15", "16", "18"} & pins
        ):
            return {
                "role": "timer",
                "confidence": 0.92,
                "reason": "Timer keywords or timed contact pins (15/16/18) found in context.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("alternating", "lead-lag", "lead_lag", "duplex")):
            return {
                "role": "alternating_relay",
                "confidence": 0.90,
                "reason": "Alternating/duplex relay context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("motor", "contactor", "main_contactor", "hauptschütz", "leistungsschütz", "schütz", "ac3", "3-phase", "starter")) or (
            {"1", "2", "3", "4", "5", "6"} <= pins
        ) or ({"L1", "L2", "L3", "T1", "T2", "T3"} <= pins):
            return {
                "role": "contactor",
                "confidence": 0.91,
                "reason": "Motor/power contactor context or 3-pole power pins detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("control relay", "control_relay", "interposing", "auxiliary", "hilfsschütz", "steuerrelais", "plc output", "logic", "relay")):
            return {
                "role": "relay",
                "confidence": 0.88,
                "reason": "Control/interposing relay context detected.",
                "model": model,
            }

    if prefix in {"Q", "QF", "QM", "QS", "CB", "MCP", "DISC", "DS"}:
        if any(tok in text_blob for tok in ("disconnect", "isolator", "hauptschalter", "lasttrennschalter", "main switch")):
            return {
                "role": "disconnect",
                "confidence": 0.92,
                "reason": "Disconnect / isolator switch context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("mpcb", "motor protection", "motorschutzschalter", "mcp")):
            return {
                "role": "mpcb",
                "confidence": 0.91,
                "reason": "Motor protection circuit breaker context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("breaker", "mcb", "mccb", "leitungsschutzschalter", "circuit breaker", "feeder")):
            return {
                "role": "breaker",
                "confidence": 0.89,
                "reason": "Circuit breaker context detected.",
                "model": model,
            }

    if prefix in {"F", "FU", "FR", "OL"}:
        if any(tok in text_blob for tok in ("overload", "thermal", "überlast", "motorschutzrelais", "ol_contact", "95", "96")) or (
            {"95", "96"} <= pins
        ):
            return {
                "role": "overload",
                "confidence": 0.92,
                "reason": "Thermal overload context or 95-96 trip pins detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("fuse", "sicherung", "class cc", "class j", "fu")):
            return {
                "role": "fuse",
                "confidence": 0.91,
                "reason": "Fuse protection context detected.",
                "model": model,
            }

    if prefix in {"S", "SB", "SA", "SF", "SQ", "SL", "SP", "ST", "B"}:
        if any(tok in text_blob for tok in ("estop", "e-stop", "emergency stop", "not-halt", "not-aus", "mushroom")):
            return {
                "role": "estop",
                "confidence": 0.94,
                "reason": "Emergency stop context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("selector", "hoa", "hand-off-auto", "wahlschalter")):
            return {
                "role": "selector",
                "confidence": 0.90,
                "reason": "Selector switch context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("limit", "endschalter", "positionsschalter")):
            return {
                "role": "limit_switch",
                "confidence": 0.90,
                "reason": "Limit switch context detected.",
                "model": model,
            }
        if any(tok in text_blob for tok in ("pushbutton", "start", "stop", "reset", "drucktaster", "taster")):
            return {
                "role": "pushbutton",
                "confidence": 0.88,
                "reason": "Pushbutton context detected.",
                "model": model,
            }

    return {
        "role": "",
        "confidence": 0.35,
        "reason": f"Insufficient context to disambiguate {comp_id or prefix}.",
        "model": model,
    }


def _heuristic_symbol_infer(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    src_lib = str(payload.get("library_id") or "").strip()
    target = target_standard(str(payload.get("target") or "NEMA/ANSI"))
    target_prefix = "nema." if target == "NEMA/ANSI" else "iec."
    candidates = payload.get("candidate_libraries") or []
    wired_pins = [str(p) for p in (payload.get("wired_pins") or [])]

    # Check if stripping vendor/custom segment matches a builtin library id
    parts = src_lib.split(".")
    if len(parts) >= 2:
        tail = ".".join(parts[1:])
        direct = f"{target_prefix}{tail}"
        for cand in candidates:
            if isinstance(cand, dict) and cand.get("library_id") == direct:
                return {
                    "library_id": direct,
                    "pin_map": {},
                    "confidence": 0.88,
                    "reason": f"Matched family tail {tail} in {target} catalog.",
                    "model": model,
                }
    return {
        "library_id": "",
        "pin_map": {},
        "confidence": 0.0,
        "reason": f"No confident counterpart for {src_lib} covering pins {wired_pins}.",
        "model": model,
    }


def _heuristic_note_infer(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    text = str(payload.get("text") or "")
    target = target_standard(str(payload.get("target") or "NEMA/ANSI"))
    glossary = IEC_TO_NEMA_GLOSSARY if target == "NEMA/ANSI" else NEMA_TO_IEC_GLOSSARY
    if not text.strip():
        return {"text": text, "confidence": 0.0, "reason": "empty text", "model": model}

    # Exact glossary match first
    stripped = text.strip()
    if stripped in glossary:
        replacement = glossary[stripped]
        return {
            "text": text.replace(stripped, replacement),
            "confidence": 0.95,
            "reason": f"Translated '{stripped}' -> '{replacement}' per {target} engineering glossary.",
            "model": model,
        }

    # Case-insensitive whole-term / substring glossary substitution (longest term first)
    updated = text
    matched_terms: list[tuple[str, str]] = []
    for src_term, dst_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        pattern = re.compile(r"(?<!\w)" + re.escape(src_term) + r"(?!\w)", flags=re.IGNORECASE)
        if pattern.search(updated):
            updated = pattern.sub(dst_term, updated)
            matched_terms.append((src_term, dst_term))

    if matched_terms and updated != text:
        pairs = ", ".join(f"'{s}'->'{d}'" for s, d in matched_terms)
        return {
            "text": updated,
            "confidence": 0.92,
            "reason": f"Reworded {pairs} to {target} terminology.",
            "model": model,
        }

    return {
        "text": text,
        "confidence": 0.0,
        "reason": "No standard-specific terms required rewording.",
        "model": model,
    }


def _heuristic_layout_infer(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    eligible = payload.get("eligible_candidates") or []
    if not eligible:
        return {"selected": "", "confidence": 0.0, "reason": "no eligible candidates", "model": model}
    best = min(
        eligible,
        key=lambda c: (
            int(c.get("hard_flag_count", 0)),
            int(c.get("soft_flag_count", 0)),
            int(c.get("crossings", 0)),
            int(c.get("index", 0)),
        ),
    )
    chosen_id = str(best.get("id") or "")
    return {
        "selected": chosen_id,
        "confidence": 0.90,
        "reason": (
            f"Selected candidate '{chosen_id}' with 0 hard flags, "
            f"{best.get('soft_flag_count', 0)} soft flags, and cleanest rung grouping."
        ),
        "model": model,
    }


# ---------------------------------------------------------------------------
# Capability 1: Ambiguous device role resolution
# ---------------------------------------------------------------------------


@dataclass
class RoleResolution:
    """Result of ``resolve_ambiguous_role``.

    Supports both 5-tuple unpacking ``(role, prefix, confidence, reason, model)``
    and attribute/dict access (``.accepted``, ``.valid``, ``.decision``).
    """

    role: str
    prefix: str
    confidence: float
    reason: str
    model: str
    accepted: bool = False
    valid: bool = False
    needs_review: bool = True
    decision: ConversionDecision | None = None

    def __iter__(self):
        yield self.role
        yield self.prefix
        yield self.confidence
        yield self.reason
        yield self.model

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return (self.role, self.prefix, self.confidence, self.reason, self.model)[key]
        return getattr(self, key)


def validate_role_proposal(
    role: str,
    target: str,
    *,
    source_prefix: str = "",
) -> tuple[bool, str, str]:
    """Validate an AI-proposed role against ``ROLE_PREFIX_TABLE``.

    Returns ``(is_valid, target_prefix, validation_note)``.
    """

    role_token = str(role or "").strip().lower()
    if not role_token:
        return False, "", "empty role proposal"

    entry = None
    for row in ROLE_PREFIX_TABLE:
        if str(row["role"]).lower() == role_token or role_token in row.get("role_tokens", ()):
            entry = row
            break
    if entry is None:
        return False, "", f"role {role!r} is not in ROLE_PREFIX_TABLE"

    src = source_prefix.lstrip("-=").upper()
    if src:
        allowed_iec = {str(a).upper() for a in entry.get("iec_aliases", ())} | {str(entry.get("iec_prefix", "")).upper()}
        allowed_nema = {str(a).upper() for a in entry.get("nema_aliases", ())} | {str(entry.get("nema_prefix", "")).upper()}
        if (src in SHARED_IEC_PREFIXES or src in SHARED_NEMA_PREFIXES) and src not in (allowed_iec | allowed_nema):
            return (
                False,
                "",
                f"role {entry['role']!r} is incompatible with source prefix {src!r}",
            )

    tgt = target_standard(target)
    prefix = str(entry["nema_prefix"] if tgt == "NEMA/ANSI" else entry["iec_prefix"])
    return True, prefix, ""


def resolve_ambiguous_role(
    device_or_id: Any,
    target: str = "NEMA/ANSI",
    *,
    library_id: str = "",
    pins: Iterable[str] = (),
    linked_contacts: Iterable[str] = (),
    connected_wires: Iterable[str] = (),
    connected_components: Iterable[str] = (),
    nearby_text: str | Iterable[str] = "",
    sheet_id: str = "",
    sheet: Any = None,
    chunk: str = "tags",
    project: Any = None,
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    fallback_prefix: str = "",
) -> RoleResolution:
    """Resolve an ambiguous device role (``-K``, ``-Q``, ``-F``, ``-S``, etc.).

    Validates the proposed role against ``ROLE_PREFIX_TABLE`` and gates acceptance
    on ``confidence >= confidence_threshold``.
    """

    comp_id = ""
    comp_props: dict[str, Any] = {}
    if hasattr(device_or_id, "id") and hasattr(device_or_id, "library_id"):
        comp_id = str(device_or_id.id)
        library_id = library_id or str(device_or_id.library_id or "")
        comp_props = dict(getattr(device_or_id, "properties", {}) or {})
    elif isinstance(device_or_id, dict):
        comp_id = str(device_or_id.get("id") or device_or_id.get("component_id") or "")
        library_id = library_id or str(device_or_id.get("library_id") or "")
        comp_props = dict(device_or_id.get("properties") or {})
    else:
        comp_id = str(device_or_id or "")

    src_prefix, suffix = split_prefix(comp_id)
    bare_prefix = src_prefix.lstrip("-=").upper()
    tgt = target_standard(target)
    default_prefix = fallback_prefix or role_prefix_for(tgt, source_prefix=bare_prefix) or bare_prefix

    if isinstance(nearby_text, (list, tuple, set)):
        nearby_str = " ".join(str(x) for x in nearby_text if x)
    else:
        nearby_str = str(nearby_text or "")

    if sheet is not None and not sheet_id:
        sheet_id = str(getattr(sheet, 'id', '') or '')
    pin_list = [str(p) for p in pins]
    contact_list = [str(c) for c in linked_contacts]
    wire_list = [str(w) for w in connected_wires] + [str(c) for c in connected_components]

    if project is not None and comp_id:
        for sh in getattr(project, "sheets", []) or []:
            if not sheet_id:
                for c in getattr(sh, "components", []) or []:
                    if c.id == comp_id:
                        sheet_id = sh.id
                        break
            for w in getattr(sh, "wires", []) or []:
                for end in (str(w.frm or ""), str(w.to or "")):
                    if end.startswith(f"{comp_id}."):
                        p_id = end.split(".", 1)[1]
                        if p_id not in pin_list:
                            pin_list.append(p_id)
                        w_desc = str(w.label or w.net or w.number or "")
                        if w_desc and w_desc not in wire_list:
                            wire_list.append(w_desc)

    active_backend = resolve_ai_backend(backend, mode="ai_assisted", ai_assist=True)
    payload: dict[str, Any] = {
        "id": comp_id,
        "component_id": comp_id,
        "source_prefix": bare_prefix,
        "library_id": library_id,
        "pins": pin_list,
        "linked_contacts": contact_list,
        "connected_wires": wire_list,
        "nearby_text": nearby_str,
        "description": str(comp_props.get("description") or ""),
        "label": str(comp_props.get("label") or ""),
        "function_text": str(comp_props.get("function_text") or comp_props.get("note") or ""),
        "target": tgt,
    }
    raw = active_backend.infer("resolve_role", payload) if active_backend.available() else {}
    proposed_role = str(raw.get("role") or "").strip().lower()
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "").strip()
    model = str(raw.get("model") or active_backend.model or "recorded-json/v1")

    valid, validated_prefix, validation_note = validate_role_proposal(
        proposed_role,
        tgt,
        source_prefix=bare_prefix,
    )
    accepted = bool(valid and confidence >= float(confidence_threshold))
    canonical_role = str(role_entry_for(role=proposed_role)["role"]) if valid else ""
    effective_role = (proposed_role or canonical_role) if valid else ""
    effective_prefix = validated_prefix if accepted else default_prefix
    needs_review = not accepted

    if not valid and validation_note:
        reason = f"{reason} (rejected: {validation_note})".strip()
    elif not accepted and confidence < float(confidence_threshold):
        reason = (
            f"{reason} (confidence {confidence:.2f} below threshold {confidence_threshold:.2f})".strip()
        )

    new_id = f"{effective_prefix}{suffix}" if suffix else effective_prefix
    proposed_id = f"{validated_prefix}{suffix}" if (validated_prefix and suffix) else (validated_prefix or comp_id)
    decision = ConversionDecision(
        chunk=chunk or "tags",
        kind="tag_rename",
        reason_code="ambiguous_role",
        sheet_id=sheet_id,
        component_id=comp_id,
        before={"id": comp_id, "prefix": bare_prefix},
        after={"id": new_id, "prefix": effective_prefix, "role": effective_role if accepted else ""},
        needs_review=needs_review,
        note=reason or f"Ambiguous prefix {bare_prefix} on {comp_id}",
        model=model,
        confidence=confidence,
        proposal={
            "role": proposed_role,
            "prefix": validated_prefix or str(raw.get("prefix") or ""),
            "id": proposed_id,
            "valid": valid,
            "accepted": accepted,
        },
        reason=reason,
    )
    return RoleResolution(
        role=effective_role if accepted else proposed_role,
        prefix=effective_prefix,
        confidence=confidence,
        reason=reason,
        model=model,
        accepted=accepted,
        valid=valid,
        needs_review=needs_review,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Capability 2: Unmapped symbol counterpart suggestion
# ---------------------------------------------------------------------------


@dataclass
class SymbolSuggestion:
    """Result of ``suggest_symbol_counterpart``."""

    source_library_id: str
    suggested_library_id: str
    effective_library_id: str
    pin_map: dict[str, str] = field(default_factory=dict)
    proposed_pin_map: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    model: str = ""
    valid: bool = False
    accepted: bool = False
    needs_review: bool = True
    rejection_reason: str = ""
    decision: ConversionDecision | None = None


def _default_pin_bridge(
    required_pins: set[str],
    target_pins: set[str],
    target: str,
) -> dict[str, str]:
    """Infer standard IEC<->NEMA contact/motor/disconnect pin pairs when unambiguous."""

    if required_pins <= target_pins:
        return {}
    tgt = target_standard(target)
    iec_to_nema_pairs = {
        "13": "3",
        "14": "4",
        "11": "1",
        "12": "2",
        "21": "1",
        "22": "2",
        "U1": "T1",
        "V1": "T2",
        "W1": "T3",
        "U2": "T4",
        "V2": "T5",
        "W2": "T6",
    }
    nema_to_iec_pairs = {v: k for k, v in iec_to_nema_pairs.items() if k not in {"11", "12"}}
    table = iec_to_nema_pairs if tgt == "NEMA/ANSI" else nema_to_iec_pairs
    bridge: dict[str, str] = {}
    for p in sorted(required_pins):
        if p in target_pins:
            continue
        mapped = table.get(p)
        if mapped and mapped in target_pins:
            bridge[p] = mapped
    return bridge


def suggest_symbol_counterpart(
    component_or_lib: Any,
    target_or_lib: str | None = None,
    *,
    target: str = "NEMA/ANSI",
    catalog: Any = None,
    wired_pins: Iterable[str] | None = None,
    sheet: Any = None,
    sheet_id: str = "",
    chunk: str = "",
    component_id: str = "",
    nearby_text: str = "",
    project: Any = None,
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> SymbolSuggestion:
    """Suggest a target-standard library symbol when ``standard_map`` has no entry.

    Hard code validation:
    1. Suggested ``library_id`` must exist in ``catalog`` (``Library.load_builtin()``)
       and belong to the target standard.
    2. Its pin set (after ``pin_map``) must cover every wired pin of the component
       (``wired_pins <= mapped_target_pins``) without colliding two wired pins onto
       the same target pin.
    """

    if catalog is None:
        from panelcad.library.catalog import Library

        catalog = Library.load_builtin()

    if target_or_lib is not None:
        low_arg = str(target_or_lib).strip().lower()
        if low_arg in {"iec", "nema", "nema/ansi", "ansi"}:
            target = str(target_or_lib)
        elif "." in str(target_or_lib) and not hasattr(component_or_lib, "library_id"):
            component_id = component_id or str(component_or_lib or "")
            component_or_lib = str(target_or_lib)
        else:
            target = str(target_or_lib)

    if hasattr(component_or_lib, "library_id"):
        src_lib = str(component_or_lib.library_id or "")
        component_id = component_id or str(getattr(component_or_lib, "id", "") or "")
    elif isinstance(component_or_lib, dict):
        src_lib = str(component_or_lib.get("library_id") or "")
        component_id = component_id or str(component_or_lib.get("id") or "")
    else:
        src_lib = str(component_or_lib or "")

    tgt = target_standard(target)
    tgt_prefix = "nema." if tgt == "NEMA/ANSI" else "iec."

    if wired_pins is not None:
        req_pins: set[str] = {str(p) for p in wired_pins if str(p)}
    else:
        req_pins = set()
        if project is not None and component_id:
            prefix = f"{component_id}."
            for sh in getattr(project, "sheets", []) or []:
                if sheet_id and sh.id != sheet_id:
                    continue
                for w in getattr(sh, "wires", []) or []:
                    for end in (str(w.frm or ""), str(w.to or "")):
                        if end.startswith(prefix):
                            req_pins.add(end[len(prefix) :])

    if not req_pins:
        try:
            src_dev = catalog.get(src_lib)
            req_pins = {str(p.id) for p in src_dev.pins}
        except KeyError:
            pass

    target_candidates: list[dict[str, Any]] = []
    devices_iter = getattr(catalog, "devices", {})
    if isinstance(devices_iter, dict):
        dev_values = list(devices_iter.values())
    else:
        dev_values = list(devices_iter)
    for dev in dev_values:
        lid = str(getattr(dev, "library_id", "") or "")
        if lid.startswith(tgt_prefix) or normalize_standard(str(getattr(dev, "standard", "")), default="") == tgt:
            target_candidates.append(
                {
                    "library_id": lid,
                    "name": str(getattr(dev, "name", "") or ""),
                    "category": str(getattr(dev, "category", "") or ""),
                    "pins": [str(p.id) for p in getattr(dev, "pins", [])],
                }
            )

    active_backend = resolve_ai_backend(backend, mode="ai_assisted", ai_assist=True)
    payload: dict[str, Any] = {
        "id": component_id or src_lib,
        "component_id": component_id,
        "library_id": src_lib,
        "wired_pins": sorted(req_pins),
        "nearby_text": nearby_text,
        "target": tgt,
        "candidate_libraries": target_candidates,
    }
    raw = active_backend.infer("suggest_symbol", payload) if active_backend.available() else {}
    suggested_lib = str(raw.get("library_id") or raw.get("suggested_library_id") or "").strip()
    raw_pin_map = dict(raw.get("pin_map") or {})
    proposed_pin_map = {str(k): str(v) for k, v in raw_pin_map.items()}
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "").strip()
    model = str(raw.get("model") or active_backend.model or "recorded-json/v1")

    valid = True
    rejection_reason = ""
    target_dev = None
    if not suggested_lib:
        valid = False
        rejection_reason = "empty symbol suggestion"
    else:
        try:
            target_dev = catalog.get(suggested_lib)
        except KeyError:
            valid = False
            rejection_reason = f"library_id {suggested_lib!r} does not exist in catalog"

    if valid and target_dev is not None:
        dev_std = normalize_standard(str(getattr(target_dev, "standard", "") or ""), default="")
        if not suggested_lib.startswith(tgt_prefix) and dev_std != tgt:
            valid = False
            rejection_reason = f"library_id {suggested_lib!r} is not in target standard {tgt}"

    effective_pin_map: dict[str, str] = dict(proposed_pin_map)
    if valid and target_dev is not None:
        target_pin_ids = {str(p.id) for p in target_dev.pins}
        if not effective_pin_map and req_pins and not (req_pins <= target_pin_ids):
            effective_pin_map = _default_pin_bridge(req_pins, target_pin_ids, tgt)
        missing_pins = sorted(p for p in req_pins if effective_pin_map.get(p, p) not in target_pin_ids)
        mapped_targets = [effective_pin_map.get(p, p) for p in req_pins]
        if missing_pins:
            valid = False
            rejection_reason = f"missing wired pins on {suggested_lib}: {', '.join(missing_pins)}"
        elif len(set(mapped_targets)) != len(mapped_targets):
            valid = False
            rejection_reason = f"pin_map collides multiple wired pins onto the same pin on {suggested_lib}"

    accepted = bool(valid and confidence >= float(confidence_threshold))
    needs_review = not accepted
    effective_lib = suggested_lib if accepted else src_lib
    final_pin_map = {k: v for k, v in effective_pin_map.items() if k != v} if accepted else {}

    if not valid and rejection_reason:
        note = f"{src_lib} has no {tgt} symbol in standard_map (AI proposal rejected: {rejection_reason})"
    elif not accepted:
        note = (
            f"{src_lib} has no {tgt} symbol in standard_map "
            f"(AI confidence {confidence:.2f} below {confidence_threshold:.2f})"
        )
    else:
        note = reason or f"Mapped {src_lib} -> {suggested_lib} via validated AI suggestion"

    decision = ConversionDecision(
        chunk=chunk or (f"{sheet_id}:symbols" if sheet_id else "symbols"),
        kind="symbol_swap",
        reason_code="unmapped_symbol",
        sheet_id=sheet_id,
        component_id=component_id,
        before={"library_id": src_lib, "wired_pins": sorted(req_pins)},
        after={
            "library_id": effective_lib,
            "pin_map": dict(final_pin_map),
        },
        needs_review=needs_review,
        note=note,
        model=model,
        confidence=confidence,
        proposal={
            "library_id": suggested_lib,
            "pin_map": dict(proposed_pin_map or effective_pin_map),
            "valid": valid,
            "accepted": accepted,
            "rejection_reason": rejection_reason,
        },
        reason=reason or rejection_reason,
    )
    return SymbolSuggestion(
        source_library_id=src_lib,
        suggested_library_id=suggested_lib,
        effective_library_id=effective_lib,
        pin_map=final_pin_map,
        proposed_pin_map=proposed_pin_map,
        confidence=confidence,
        reason=f'{reason} ({rejection_reason})'.strip() if (not valid and rejection_reason) else (reason or rejection_reason),
        model=model,
        valid=valid,
        accepted=accepted,
        needs_review=needs_review,
        rejection_reason=rejection_reason,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Capability 3: Note, callout, and title text rewording
# ---------------------------------------------------------------------------


@dataclass
class NoteRewordResult:
    """Result of rewording a single note, callout, or sheet title."""

    original: str
    proposal: str
    effective_text: str
    confidence: float
    reason: str
    model: str
    accepted: bool
    needs_review: bool
    decision: ConversionDecision


def reword_note_text(
    text: str,
    target: str = "NEMA/ANSI",
    *,
    sheet_id: str = "",
    component_id: str = "",
    field_name: str = "note",
    chunk: str = "tags",
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> NoteRewordResult | None:
    """Reword one IEC/NEMA note, callout, or title string if applicable."""

    original = str(text or "")
    if not original.strip():
        return None

    tgt = target_standard(target)
    active_backend = resolve_ai_backend(backend, mode="ai_assisted", ai_assist=True)
    if not active_backend.available():
        return None

    payload = {
        "id": f"{sheet_id}:{component_id}:{field_name}".strip(":"),
        "text": original,
        "sheet_id": sheet_id,
        "component_id": component_id,
        "field": field_name,
        "target": tgt,
    }
    raw = active_backend.infer("reword_note", payload)
    proposed = str(raw.get("text") or raw.get("proposal") or original)
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "").strip()
    model = str(raw.get("model") or active_backend.model or "recorded-json/v1")

    if proposed == original and confidence == 0.0:
        return None

    accepted = bool(proposed.strip() and proposed != original and confidence >= float(confidence_threshold))
    effective = proposed if accepted else original
    needs_review = not accepted

    decision = ConversionDecision(
        chunk=chunk,
        kind="note_reword",
        reason_code="note_reworded",
        sheet_id=sheet_id,
        component_id=component_id,
        before={"field": field_name, "text": original, "original": original},
        after={"field": field_name, "text": effective},
        needs_review=needs_review,
        note=reason or f"Reworded {field_name}: '{original}' -> '{effective}'",
        model=model,
        confidence=confidence,
        proposal={
            "field": field_name,
            "original": original,
            "proposal": proposed,
            "text": proposed,
            "accepted": accepted,
        },
        reason=reason,
    )
    return NoteRewordResult(
        original=original,
        proposal=proposed,
        effective_text=effective,
        confidence=confidence,
        reason=reason,
        model=model,
        accepted=accepted,
        needs_review=needs_review,
        decision=decision,
    )


def reword_sheet_notes(
    target_obj: Any,
    target: str = "NEMA/ANSI",
    *,
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    apply: bool = True,
    chunk: str = "tags",
    sheets: Iterable[str] | None = None,
) -> list[ConversionDecision]:
    """Translate/reword IEC <-> NEMA sheet titles, descriptions, component callouts, and notes.

    Preserves the original text in each ``ConversionDecision`` (``before["original"]``
    and ``proposal["original"]``) and in ``_convert_original_notes`` so reverting
    restores the exact pre-conversion wording.
    """

    decisions: list[ConversionDecision] = []
    if hasattr(target_obj, "project"):
        target_obj = target_obj.project

    if isinstance(target_obj, str):
        res = reword_note_text(
            target_obj,
            target,
            field_name="note",
            chunk=chunk,
            backend=backend,
            confidence_threshold=confidence_threshold,
        )
        return [res.decision] if res is not None else []

    sheet_filter = {str(s) for s in sheets} if sheets is not None else None
    sheet_objs: list[Any] = []
    if hasattr(target_obj, "sheets"):
        sheet_objs = [
            s
            for s in (getattr(target_obj, "sheets", []) or [])
            if sheet_filter is None or str(getattr(s, "id", "")) in sheet_filter
        ]
    elif hasattr(target_obj, "components") and hasattr(target_obj, "id"):
        sheet_objs = [target_obj]
    elif isinstance(target_obj, list):
        for idx, item in enumerate(target_obj):
            if isinstance(item, str):
                res = reword_note_text(
                    item,
                    target,
                    field_name=f"note[{idx}]",
                    chunk=chunk,
                    backend=backend,
                    confidence_threshold=confidence_threshold,
                )
                if res is not None:
                    if apply and res.accepted:
                        target_obj[idx] = res.effective_text
                    decisions.append(res.decision)
            elif isinstance(item, dict) and "text" in item:
                res = reword_note_text(
                    str(item.get("text") or ""),
                    target,
                    field_name=str(item.get("id") or f"note[{idx}]"),
                    chunk=chunk,
                    backend=backend,
                    confidence_threshold=confidence_threshold,
                )
                if res is not None:
                    if apply and res.accepted:
                        item.setdefault("original_text", res.original)
                        item["text"] = res.effective_text
                    decisions.append(res.decision)
        return decisions

    proj_props = getattr(target_obj, "properties", None) if hasattr(target_obj, "sheets") else None
    if isinstance(proj_props, dict):
        default_sid = str(getattr(sheet_objs[0], "id", "") if sheet_objs else "")
        for key in _NOTE_PROP_KEYS:
            val = proj_props.get(key)
            if isinstance(val, str) and val.strip():
                res = reword_note_text(
                    val,
                    target,
                    sheet_id=default_sid,
                    field_name=f"project.properties.{key}",
                    chunk=chunk,
                    backend=backend,
                    confidence_threshold=confidence_threshold,
                )
                if res is not None:
                    if apply and res.accepted:
                        orig_map = proj_props.setdefault("_convert_original_notes", {})
                        if isinstance(orig_map, dict):
                            orig_map.setdefault(key, res.original)
                        proj_props[key] = res.effective_text
                    decisions.append(res.decision)
            elif isinstance(val, list):
                orig_list = list(val)
                changed_any = False
                for idx, item in enumerate(val):
                    if not isinstance(item, str) or not item.strip():
                        continue
                    res = reword_note_text(
                        item,
                        target,
                        sheet_id=default_sid,
                        field_name=f"project.properties.{key}[{idx}]",
                        chunk=chunk,
                        backend=backend,
                        confidence_threshold=confidence_threshold,
                    )
                    if res is not None:
                        if apply and res.accepted:
                            val[idx] = res.effective_text
                            changed_any = True
                        decisions.append(res.decision)
                if changed_any:
                    orig_map = proj_props.setdefault("_convert_original_notes", {})
                    if isinstance(orig_map, dict):
                        orig_map.setdefault(key, orig_list)

    for sh in sheet_objs:
        sid = str(getattr(sh, "id", "") or "")
        for attr in ("title", "description"):
            val = str(getattr(sh, attr, "") or "")
            if not val.strip():
                continue
            res = reword_note_text(
                val,
                target,
                sheet_id=sid,
                field_name=f"sheet.{attr}",
                chunk=chunk,
                backend=backend,
                confidence_threshold=confidence_threshold,
            )
            if res is not None:
                if apply and res.accepted:
                    sh_orig = sh.__dict__.setdefault("_convert_original_notes", {})
                    sh_orig.setdefault(attr, res.original)
                    setattr(sh, attr, res.effective_text)
                decisions.append(res.decision)

        sh_props = getattr(sh, "properties", None)
        if isinstance(sh_props, dict):
            for key in _NOTE_PROP_KEYS:
                val = sh_props.get(key)
                if isinstance(val, str) and val.strip():
                    res = reword_note_text(
                        val,
                        target,
                        sheet_id=sid,
                        field_name=f"sheet.properties.{key}",
                        chunk=chunk,
                        backend=backend,
                        confidence_threshold=confidence_threshold,
                    )
                    if res is not None:
                        if apply and res.accepted:
                            orig_map = sh_props.setdefault("_convert_original_notes", {})
                            if isinstance(orig_map, dict):
                                orig_map.setdefault(key, res.original)
                            sh_props[key] = res.effective_text
                        decisions.append(res.decision)
                elif isinstance(val, list):
                    orig_list = list(val)
                    changed_any = False
                    for idx, item in enumerate(val):
                        if not isinstance(item, str) or not item.strip():
                            continue
                        res = reword_note_text(
                            item,
                            target,
                            sheet_id=sid,
                            field_name=f"sheet.properties.{key}[{idx}]",
                            chunk=chunk,
                            backend=backend,
                            confidence_threshold=confidence_threshold,
                        )
                        if res is not None:
                            if apply and res.accepted:
                                val[idx] = res.effective_text
                                changed_any = True
                            decisions.append(res.decision)
                    if changed_any:
                        orig_map = sh_props.setdefault("_convert_original_notes", {})
                        if isinstance(orig_map, dict):
                            orig_map.setdefault(key, orig_list)

        for comp in getattr(sh, "components", []) or []:
            props = getattr(comp, "properties", None)
            if not isinstance(props, dict):
                continue
            for key in _NOTE_PROP_KEYS:
                val = props.get(key)
                if not isinstance(val, str) or not val.strip():
                    continue
                res = reword_note_text(
                    val,
                    target,
                    sheet_id=sid,
                    component_id=comp.id,
                    field_name=key,
                    chunk=chunk,
                    backend=backend,
                    confidence_threshold=confidence_threshold,
                )
                if res is not None:
                    if apply and res.accepted:
                        orig_map = props.get("_convert_original_notes")
                        if not isinstance(orig_map, dict):
                            orig_map = {}
                            props["_convert_original_notes"] = orig_map
                        orig_map.setdefault(key, res.original)
                        props[key] = res.effective_text
                    decisions.append(res.decision)

    return decisions


def revert_reworded_notes(
    target_obj: Any,
    decisions: Iterable[ConversionDecision | dict[str, Any]] | None = None,
    *,
    sheets: Iterable[str] | None = None,
) -> int:
    """Restore original note/callout/title text from ``decisions`` or ``_convert_original_notes``."""

    if hasattr(target_obj, "project"):
        target_obj = target_obj.project
    sheet_filter = {str(s) for s in sheets} if sheets is not None else None
    sheet_objs: list[Any] = []
    if hasattr(target_obj, "sheets"):
        sheet_objs = [
            s
            for s in (getattr(target_obj, "sheets", []) or [])
            if sheet_filter is None or str(getattr(s, "id", "")) in sheet_filter
        ]
    elif hasattr(target_obj, "components") and hasattr(target_obj, "id"):
        sheet_objs = [target_obj]

    restored = 0
    if decisions is not None:
        by_sheet = {str(getattr(s, "id", "")): s for s in sheet_objs}
        for raw_d in decisions:
            d = ConversionDecision.from_dict(raw_d) if isinstance(raw_d, dict) else raw_d
            if d.reason_code != "note_reworded":
                continue
            orig = str(d.before.get("original") or d.before.get("text") or d.proposal.get("original") or "")
            field_name = str(d.before.get("field") or d.proposal.get("field") or "")
            sh = by_sheet.get(d.sheet_id) or (sheet_objs[0] if len(sheet_objs) == 1 else None)
            if sh is None:
                continue
            if field_name in {"sheet.title", "title"}:
                sh.title = orig
                restored += 1
            elif field_name in {"sheet.description", "description"} and not d.component_id:
                sh.description = orig
                restored += 1
            elif d.component_id:
                for comp in getattr(sh, "components", []) or []:
                    if comp.id == d.component_id and isinstance(comp.properties, dict):
                        key = field_name or "note"
                        comp.properties[key] = orig
                        if isinstance(comp.properties.get("_convert_original_notes"), dict):
                            comp.properties["_convert_original_notes"].pop(key, None)
                            if not comp.properties["_convert_original_notes"]:
                                comp.properties.pop("_convert_original_notes", None)
                        restored += 1
        return restored

    proj_props = getattr(target_obj, "properties", None) if hasattr(target_obj, "sheets") else None
    if isinstance(proj_props, dict):
        p_orig = proj_props.pop("_convert_original_notes", None)
        if isinstance(p_orig, dict):
            for k, v in p_orig.items():
                proj_props[k] = list(v) if isinstance(v, list) else v
                restored += 1

    for sh in sheet_objs:
        sh_orig = sh.__dict__.pop("_convert_original_notes", None)
        if isinstance(sh_orig, dict):
            for k, v in sh_orig.items():
                setattr(sh, k, v)
                restored += 1
        sh_props = getattr(sh, "properties", None)
        if isinstance(sh_props, dict):
            sh_prop_orig = sh_props.pop("_convert_original_notes", None)
            if isinstance(sh_prop_orig, dict):
                for k, v in sh_prop_orig.items():
                    sh_props[k] = list(v) if isinstance(v, list) else v
                    restored += 1
        for comp in getattr(sh, "components", []) or []:
            props = getattr(comp, "properties", None)
            if not isinstance(props, dict):
                continue
            orig_map = props.pop("_convert_original_notes", None)
            if isinstance(orig_map, dict):
                for k, v in orig_map.items():
                    props[k] = v
                    restored += 1
    return restored


# ---------------------------------------------------------------------------
# Capability 4: Ladder layout candidate ranking
# ---------------------------------------------------------------------------


@dataclass
class LayoutRankingResult:
    """Result of ``rank_layout_candidates``."""

    selected_id: str
    selected_candidate: dict[str, Any]
    disqualified_ids: list[str]
    eligible_ids: list[str]
    scored_candidates: list[dict[str, Any]]
    confidence: float
    reason: str
    model: str
    accepted: bool
    needs_review: bool
    decision: ConversionDecision

    @property
    def chosen_id(self) -> str:
        return self.selected_id


def rank_layout_candidates(
    candidates_or_session: Any,
    sheet_id_pos: str | None = None,
    candidates_pos: list[dict[str, Any]] | None = None,
    *,
    session: Any = None,
    target: str = "NEMA/ANSI",
    sheet_id: str = "",
    chunk: str = "",
    backend: ConversionAIBackend | dict[str, Any] | Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> LayoutRankingResult:
    """Score layout candidates with ``sheet_flags``, disqualify hard-flagged ones,
    and rank the valid candidates with the AI advisor.
    """

    from panelcad.convert.flags import sheet_flags

    if isinstance(candidates_or_session, list):
        candidates = candidates_or_session
    else:
        session = candidates_or_session if session is None else session
        if sheet_id_pos is not None and not sheet_id:
            sheet_id = str(sheet_id_pos)
        candidates = list(candidates_pos or [])

    scored: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        c_dict = dict(cand)
        cid = str(c_dict.get("id") or f"candidate_{idx}")
        c_dict["id"] = cid
        c_dict["index"] = idx

        flags = c_dict.get("flags")
        if flags is None and session is not None:
            cand_sid = str(c_dict.get("sheet_id") or sheet_id or "")
            if cand_sid:
                flags = sheet_flags(session, cand_sid)
        flag_list = list(flags or [])
        hard_flags = [
            str(f.get("flag") if isinstance(f, dict) else f)
            for f in flag_list
            if str(f.get("flag") if isinstance(f, dict) else f) in HARD_LAYOUT_FLAGS
        ]
        soft_flags = [
            str(f.get("flag") if isinstance(f, dict) else f)
            for f in flag_list
            if str(f.get("flag") if isinstance(f, dict) else f) not in HARD_LAYOUT_FLAGS
        ]
        c_dict["flags"] = flag_list
        c_dict["hard_flags"] = hard_flags
        c_dict["soft_flags"] = soft_flags
        c_dict["hard_flag_count"] = len(hard_flags)
        c_dict["soft_flag_count"] = len(soft_flags)
        c_dict["disqualified"] = len(hard_flags) > 0
        scored.append(c_dict)

    disqualified_ids = [c["id"] for c in scored if c["disqualified"]]
    eligible = [c for c in scored if not c["disqualified"]]
    all_disqualified = False
    if not eligible and scored:
        all_disqualified = True
        min_hard = min(c["hard_flag_count"] for c in scored)
        eligible = [c for c in scored if c["hard_flag_count"] == min_hard]

    eligible_ids = [c["id"] for c in eligible]
    deterministic_best = (
        min(
            eligible,
            key=lambda c: (
                c["hard_flag_count"],
                c["soft_flag_count"],
                int(c.get("crossings", 0)),
                c["index"],
            ),
        )
        if eligible
        else {"id": "", "hard_flag_count": 0, "soft_flag_count": 0}
    )

    active_backend = resolve_ai_backend(backend, mode="ai_assisted", ai_assist=True)
    payload = {
        "id": sheet_id or "layout",
        "sheet_id": sheet_id,
        "target": target_standard(target),
        "eligible_candidates": [
            {
                "id": c["id"],
                "index": c["index"],
                "hard_flag_count": c["hard_flag_count"],
                "soft_flag_count": c["soft_flag_count"],
                "crossings": int(c.get("crossings", 0)),
                "description": str(c.get("description") or ""),
            }
            for c in eligible
        ],
        "disqualified_ids": disqualified_ids,
    }
    raw = active_backend.infer("rank_layout", payload) if (active_backend.available() and eligible) else {}
    proposed_id = str(raw.get("selected") or raw.get("chosen_id") or "").strip()
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "").strip()
    model = str(raw.get("model") or active_backend.model or "recorded-json/v1")

    by_eligible_id = {c["id"]: c for c in eligible}
    valid_choice = bool(proposed_id and proposed_id in by_eligible_id)
    accepted = bool(valid_choice and not all_disqualified and confidence >= float(confidence_threshold))
    if accepted:
        selected_cand = by_eligible_id[proposed_id]
    else:
        selected_cand = deterministic_best
        if proposed_id and not valid_choice:
            reason = f"{reason} (rejected disqualified/invalid candidate {proposed_id!r})".strip()

    selected_id = str(selected_cand.get("id") or "")
    needs_review = bool(all_disqualified or not accepted)

    decision = ConversionDecision(
        chunk=chunk or (f"{sheet_id}:layout" if sheet_id else "layout"),
        kind="layout_ranking",
        reason_code="layout_candidate",
        sheet_id=sheet_id,
        before={
            "candidates": [c["id"] for c in scored],
            "disqualified": disqualified_ids,
        },
        after={
            "selected": selected_id,
            "eligible": eligible_ids,
            "disqualified": disqualified_ids,
        },
        needs_review=needs_review,
        note=reason or f"Selected layout candidate {selected_id}",
        model=model,
        confidence=confidence,
        proposal={
            "selected": proposed_id or selected_id,
            "accepted": accepted,
            "valid_choice": valid_choice,
            "disqualified": disqualified_ids,
            "eligible": eligible_ids,
        },
        reason=reason,
    )
    return LayoutRankingResult(
        selected_id=selected_id,
        selected_candidate=selected_cand,
        disqualified_ids=disqualified_ids,
        eligible_ids=eligible_ids,
        scored_candidates=scored,
        confidence=confidence,
        reason=reason,
        model=model,
        accepted=accepted,
        needs_review=needs_review,
        decision=decision,
    )
