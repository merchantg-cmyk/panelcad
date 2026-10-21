from __future__ import annotations

import copy
import functools
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from panelcad.core.geometry import Page
from panelcad.core.layout import (
    center_in_frame,
    default_enclosure_and_rails,
    fit_enclosure_scale,
    fit_rails_between_ducts,
    fit_standard_enclosure_scale,
    footprint_size,
    place_scale,
    inner_backplate,
    is_io_sheet,
    is_layout_sheet,
    is_network_sheet,
    clip_offset,
    nearest_rail,
    pack_din,
    symbol_body_size,
    backplate_sheet_bounds,
    body_inside,
    footprint_overlaps_tray,
    format_mm,
    sits_fully_on_a_rail,
    nudge_components_apart,
    rail_aabb,
    pack_field,
    panel_furniture,
    tray_aabb,
    place_on_rail,
    rail_height,
    separate_vertically,
)
from panelcad.render.titleblock import lift_off_title_block, working_rect
from panelcad.library.panel import (
    ENVELOPE_AT_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_CONFIRMED_KEY,
    ENVELOPE_SNAPSHOT_KEYS,
    apply_confirmed_envelope,
    assigned_catalog_ready,
    assigned_catalog_ready_props,
    assignment_clears_confirmation,
    assignment_labels_match,
    assignment_preserve_instance_keys,
    catalog_row_fits_device,
    clear_envelope_confirmation,
    envelope_edit_changes,
    refuse_nonfinite_sizes,
    effective_footprint,
    envelope_is_confirmed,
    is_child_contact,
    is_incabinet,
    is_layout_ready,
    layout_ready_error,
    LayoutNotReadyError,
    occupies_panel_layout,
    raise_layout_not_ready,
    sanitize_caller_properties,
    strip_placeholder_guesses,
)
from panelcad.core.models import (
    CommPort,
    Component,
    Device,
    Enclosure,
    NetworkInfo,
    PinDef,
    PinOverride,
    Project,
    Rail,
    Revision,
    Sheet,
    SheetKind,
    WireTray,
    Wire,
    infer_pin_side,
)
from panelcad.library.ports import (
    default_kind_side,
    effective_ports,
    network_address,
    parse_port,
    port_matches_medium,
    retain_known_ip,
)
from panelcad.io import share as project_share
from panelcad.io.yaml_format import load_project, save_project
from panelcad.library.catalog import Catalog, default_catalog
from panelcad.library.parts import PartsCatalog, default_parts_catalog
from panelcad.render.reports import wire_rows, write_csv
from panelcad.render.sheet import parse_pin_ref
from panelcad.render.svg import sheet_to_svg
import panelcad.reconstruct.placeholder  # noqa: F401 — register layout_ready extras



def _grab_screenshot() -> bytes | None:
    try:
        from PySide6.QtWidgets import QApplication

        from panelcad.gui.bug_report_dialog import grab_png
    except Exception:  # noqa: BLE001
        return None
    app = QApplication.instance()
    if app is None:
        return None
    return grab_png(app.activeWindow())


def _log_io(action: str, path: object = None) -> None:
    try:
        from panelcad.diag.log import log_io

        log_io(action, path)
    except Exception:  # noqa: BLE001
        return


def _note_project(session: Session) -> None:
    try:
        from panelcad.diag.crash import note_open_project

        name = str(getattr(session.project, "name", "") or "")
        stored = absolute_project_path(session.path)
        if stored is not None:
            session.path = stored
        note_open_project(session.path, name, session=session)
    except Exception:  # noqa: BLE001
        return


def _bind_primary(session: Session) -> None:
    """Register the drawing a crash should copy. Helpers must not call this."""

    try:
        from panelcad.diag.crash import set_recovery_session

        set_recovery_session(session)
    except Exception:  # noqa: BLE001
        return


def _remember_saved(session: Session) -> None:
    try:
        from panelcad.diag.crash import remember_saved

        remember_saved(session)
    except Exception:  # noqa: BLE001
        return


def _safe_io_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if "\n" in text or len(text) > 180:
        return type(exc).__name__
    return text


def _traced_path(action: str):
    """Log an import or export path. Never log the file body."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, path, *args, **kwargs):
            try:
                result = fn(self, path, *args, **kwargs)
            except Exception as exc:
                _log_io("error", f"{action} {_safe_io_error(exc)}")
                raise
            logged = result if isinstance(result, Path) else path
            _log_io(action, logged)
            return result

        return wrapper

    return deco


def _project_snapshot(session: "Session") -> tuple[bytes, str]:
    """YAML of the open project written only to a temp file."""

    folder = Path(tempfile.mkdtemp(prefix="panelcad-report-"))
    dest = folder / "project.pcad.yaml"
    source = str(session.path or "")
    try:
        save_project(session.project, dest)
        return dest.read_bytes(), source
    finally:
        shutil.rmtree(folder, ignore_errors=True)


_CUTOUT_KEEP = ("include_key", "min_center_x_mm", "min_center_y_mm", "centers_interchangeable")


def _stash_cutout_props(comp: Any) -> None:
    """Remember the opening that was on the device before it was marked unknown."""

    if "cutout_restore" in comp.properties:
        return
    raw = comp.properties.get("cutout")
    prior = None
    if isinstance(raw, dict) and not raw.get("unknown"):
        prior = json.loads(json.dumps(raw))
    comp.properties["cutout_restore"] = {
        "cutout": prior,
        "include_key": comp.properties.get("include_key"),
        "min_center_x_mm": comp.properties.get("min_center_x_mm"),
        "min_center_y_mm": comp.properties.get("min_center_y_mm"),
        "centers_interchangeable": comp.properties.get("centers_interchangeable"),
    }


def _restore_cutout_props(comp: Any) -> None:
    """Put back the opening saved when Unknown was checked."""

    saved = comp.properties.pop("cutout_restore", None)
    current = comp.properties.get("cutout")
    if isinstance(current, dict) and current.get("unknown"):
        comp.properties.pop("cutout", None)
    if not isinstance(saved, dict):
        return
    prior = saved.get("cutout")
    if isinstance(prior, dict) and not prior.get("unknown"):
        comp.properties["cutout"] = prior
    for key in _CUTOUT_KEEP:
        if key not in saved:
            continue
        value = saved[key]
        if value is None:
            comp.properties.pop(key, None)
        else:
            comp.properties[key] = value


def absolute_project_path(path: Path | str | None) -> Path | None:
    """Resolve a relative project path against the folder PanelCAD was started in.

    An absolute path is kept as written, so Save still hits that file after a
    restart in another folder. A Windows drive path is left unchanged.
    """

    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    raw = Path(text)
    if raw.is_absolute() or (len(text) >= 2 and text[1] == ":"):
        return raw
    return (Path.cwd() / raw).resolve()


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    return out or "sheet"


def _as_point(raw: Any):
    from panelcad.core.geometry import Point

    if raw is None:
        return None
    if isinstance(raw, Point):
        return Point(x=float(raw.x), y=float(raw.y))
    if isinstance(raw, dict):
        return Point(x=float(raw.get("x", 0)), y=float(raw.get("y", 0)))
    return Point(x=float(raw[0]), y=float(raw[1]))


def _rail_click(sheet, project, catalog, ref: str, other: str, raw: Any):
    """Keep a rail click that is not already the straight landing on the other pin."""

    point = _as_point(raw)
    if point is None:
        return None
    from panelcad.core.ladder import explicit_rail_landing

    return explicit_rail_landing(sheet, ref, other, point, project=project, catalog=catalog)


class Session:
    """Single façade for GUI, CLI, Python, and the JSONL agent."""

    def __init__(
        self,
        project: Project,
        catalog: Catalog | None = None,
        path: Path | None = None,
        parts: PartsCatalog | None = None,
    ) -> None:
        self.project = project
        project_dir = path.parent if path else None
        self.catalog = catalog or default_catalog(project_dir)
        self.parts = parts or default_parts_catalog(project_dir)
        self.path = Path(path) if path else None
        visible = [s.id for s in project.sheets if not s.closed]
        if visible:
            self.active_sheet_id = visible[0]
        elif project.sheets:
            project.sheets[0].closed = False
            self.active_sheet_id = project.sheets[0].id
        else:
            self.active_sheet_id = ""
        self._wire_seq = 0
        self.actor: dict[str, str] = {"kind": "human", "model": "", "name": ""}
        self.activity: list[dict[str, Any]] = []
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        # Latest full snapshot for each stack. Older steps keep only the diff.
        self._undo_tip: dict[str, Any] | None = None
        self._redo_tip: dict[str, Any] | None = None
        # Bumped when the drawing changes so a sheet scene is not reused stale.
        self._drawing_generation = 0
        self._convert_workspace: Path | None = None
        self._restoring = False
        self._history_lock = 0
        self._command_name = ""
        self._command_before: dict[str, Any] | None = None
        self.recovered = False
        self.read_only = False
        self.lock_holder = None
        self.share_notice = ""
        self.share_mode = "local"
        self.conflict_copies: list[Path] = []
        self._disk_stamp = ""
        self._stamped_path: Path | None = None
        self._lock_token = ""
        self._share_summary = ""
        self._share_restore: tuple[list[Any], Any] | None = None

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    MAX_UNDO = 80

    def _history_snapshot(self) -> dict[str, Any]:
        return {
            "project": self.project.model_dump(mode="python"),
            "active_sheet_id": self.active_sheet_id,
            "wire_seq": self._wire_seq,
            "path": str(self.path) if self.path else "",
            # Replaced as a whole by print import. In-place catalog loads share
            # this object, so identity is only enough for a replacement.
            "catalog": self.catalog,
        }

    def _entry_snap(self, entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict) and isinstance(entry.get("snap"), dict):
            return entry["snap"]
        if isinstance(entry, dict) and "patch" in entry:
            for stack, tip in ((self._undo, self._undo_tip), (self._redo, self._redo_tip)):
                try:
                    index = stack.index(entry)
                except ValueError:
                    continue
                return self._materialize(stack, index, tip if index == len(stack) - 1 else None)
        return entry if isinstance(entry, dict) else {}

    def _entry_name(self, entry: Any) -> str:
        if isinstance(entry, dict):
            return str(entry.get("name") or "edit")
        return "edit"

    def undo_command_names(self) -> list[str]:
        """Names of the undo steps, oldest first. Empty when a step has no name."""

        names: list[str] = []
        for entry in self._undo:
            label = self._entry_name(entry).strip()
            if label:
                names.append(label)
        return names

    def _drawing_changed(self, before: dict[str, Any] | None) -> bool:
        """True when the project or a replaced catalog differs from ``before``.

        The active sheet, wire counter, and save path are view or file state.
        A page change by itself is not an undo step.
        """

        if not before:
            return False
        if before.get("project") != self.project.model_dump(mode="python"):
            return True
        return before.get("catalog") is not self.catalog

    def _push_undo(self, snap: dict[str, Any], name: str) -> None:
        self._store_history(self._undo, snap, name, "_undo_tip")
        self._trim_history(self._undo, "_undo_tip")
        self._redo.clear()
        self._redo_tip = None

    def _store_history(self, stack: list[dict[str, Any]], snap: dict[str, Any], name: str, tip_attr: str) -> None:
        """Append one step. Most steps store a diff against the previous snapshot."""

        from panelcad.api.history import FULL_EVERY, diff_snap

        tip = getattr(self, tip_attr)
        label = name or "edit"
        use_full = not stack or tip is None or len(stack) % FULL_EVERY == 0
        if use_full:
            stack.append({"name": label, "snap": snap})
        else:
            patch = diff_snap(tip, snap)
            if patch is None:
                stack.append({"name": label, "snap": snap})
            else:
                stack.append({"name": label, "patch": patch})
        setattr(self, tip_attr, snap)

    def _trim_history(self, stack: list[dict[str, Any]], tip_attr: str) -> None:
        extra = len(stack) - self.MAX_UNDO
        if extra <= 0:
            return
        tip = getattr(self, tip_attr) if extra == len(stack) - 1 else None
        head = self._materialize(stack, extra, tip)
        stack[extra] = {"name": self._entry_name(stack[extra]), "snap": head}
        del stack[:extra]

    def _materialize(
        self,
        stack: list[dict[str, Any]],
        index: int,
        tip: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from panelcad.api.history import apply_snap

        if index < 0 or index >= len(stack):
            return {}
        if index == len(stack) - 1 and tip is not None:
            return tip
        full_at = index
        while full_at >= 0 and not isinstance(stack[full_at].get("snap"), dict):
            full_at -= 1
        if full_at < 0:
            return {}
        current = stack[full_at]["snap"]
        for cursor in range(full_at + 1, index + 1):
            patch = stack[cursor].get("patch")
            if patch is None:
                stored = stack[cursor].get("snap")
                if isinstance(stored, dict):
                    current = stored
                continue
            current = apply_snap(current, patch)
        return current

    def _sync_history_tip(self, stack: list[dict[str, Any]], tip_attr: str) -> None:
        if not stack:
            setattr(self, tip_attr, None)
            return
        if isinstance(stack[-1].get("snap"), dict):
            setattr(self, tip_attr, stack[-1]["snap"])
            return
        setattr(self, tip_attr, self._materialize(stack, len(stack) - 1, None))

    def checkpoint(self, name: str = "edit") -> None:
        """Remember the drawing so the editor can undo. No-op during undo/redo.

        A second call with the same drawing is not a step. Prefer ``command``
        so a later no-op mutation is never pushed.
        """

        if self._restoring or self._history_lock:
            return
        snap = self._history_snapshot()
        if self._undo and self._undo_tip is not None:
            last = self._undo_tip
            if last.get("project") == snap.get("project") and last.get("catalog") is snap.get("catalog"):
                return
        self._push_undo(snap, name)

    def begin_command(self, name: str = "edit") -> bool:
        """Open one undo step. Nested calls join the step already open.

        Returns False while a restore is in progress, so the caller must not
        close a command it did not open.
        """

        if self._restoring:
            return False
        label = str(name or "edit")
        if self._history_lock:
            self._history_lock += 1
            if self._command_name in {"", "edit"} and label not in {"", "edit"}:
                self._command_name = label
            return True
        self._command_name = label
        self._command_before = self._history_snapshot()
        self._history_lock += 1
        return True

    def end_command(self) -> None:
        """Close one command level. The outer close pushes a step only if the drawing changed."""

        if self._restoring or not self._history_lock:
            return
        self._history_lock -= 1
        if self._history_lock:
            return
        before = self._command_before
        name = self._command_name or "edit"
        self._command_before = None
        self._command_name = ""
        if before is not None and self._drawing_changed(before):
            self._push_undo(before, name)
            self._invalidate_sheet_cache()

    def _cancel_command(self) -> None:
        """Drop the open command and put the drawing back when this level changed it."""

        if self._restoring or not self._history_lock:
            return
        self._history_lock -= 1
        if self._history_lock:
            return
        before = self._command_before
        self._command_before = None
        self._command_name = ""
        if before is not None and self._drawing_changed(before):
            self._restore_history(before)

    @contextmanager
    def command(self, name: str) -> Iterator[None]:
        """One named undo step. Nested ``command`` calls share this step.

        Nothing is pushed when the drawing is unchanged. A failure keeps a
        partial change on the stack so Undo can remove it.
        """

        opened = self.begin_command(name)
        try:
            yield
        finally:
            if opened:
                self.end_command()

    def _undoable(self, op: str, summary: str, fn: Callable[[], Any]) -> Any:
        """One undo step around ``fn``. A failure puts the drawing back.

        A nested call joins the command already open. A function that changes
        nothing does not push a step.
        """

        if self._restoring:
            raise RuntimeError("cannot edit while undoing")
        nested = self._history_lock > 0
        opened = self.begin_command(summary or op)
        failed = True
        try:
            result = fn()
            failed = False
        finally:
            if opened:
                if failed:
                    if nested:
                        self.end_command()
                    else:
                        self._cancel_command()
                else:
                    self.end_command()
        try:
            self.emit(op, summary)
        except Exception:  # noqa: BLE001
            pass
        return result

    def begin_edit(self, name: str = "edit") -> None:
        """One undo step for a drag (move / pin slide / wire pull)."""

        self.begin_command(name)

    def end_edit(self) -> None:
        self.end_command()

    def forget_noop_checkpoint(self) -> None:
        """Drop a step whose drawing matches the drawing on screen."""

        if self._history_lock or not self._undo:
            return
        if not self._drawing_changed(self._undo_tip or self._entry_snap(self._undo[-1])):
            self._undo.pop()
            self._sync_history_tip(self._undo, "_undo_tip")

    def command_open(self) -> bool:
        """True while a drag or other edit has not been closed."""

        return self._history_lock > 0 and not self._restoring

    def _abandon_open_command(self) -> None:
        """Drop an in-progress edit so Undo/Redo is not overwritten when it ends.

        The drawing goes back to the start of that edit. Nothing is pushed, and
        the redo list is left alone.
        """

        if self._restoring or not self._history_lock:
            return
        before = self._command_before
        self._history_lock = 0
        self._command_before = None
        self._command_name = ""
        if before is not None and self._drawing_changed(before):
            self._restore_history(before)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def _touch_drawing(self) -> None:
        """The picture on screen may be stale. Sheet scenes check this."""

        self._drawing_generation = int(getattr(self, "_drawing_generation", 0)) + 1

    def _invalidate_sheet_cache(self) -> None:
        """Routes and marks follow the model. An edit, undo, redo, or import starts over."""

        self._touch_drawing()
        try:
            from panelcad.render.label_avoid import clear_resolved_labels

            clear_resolved_labels()
        except Exception:  # noqa: BLE001
            try:
                from panelcad.render.wiring import clear_route_cache

                clear_route_cache()
            except Exception:  # noqa: BLE001
                return

    def undo(self) -> bool:
        self._abandon_open_command()
        if not self._undo:
            return False
        name = self._entry_name(self._undo[-1])
        restore = self._undo_tip if self._undo_tip is not None else self._entry_snap(self._undo[-1])
        self._undo.pop()
        self._sync_history_tip(self._undo, "_undo_tip")
        self._store_history(self._redo, self._history_snapshot(), name, "_redo_tip")
        self._trim_history(self._redo, "_redo_tip")
        self._restore_history(restore)
        return True

    def redo(self) -> bool:
        self._abandon_open_command()
        if not self._redo:
            return False
        name = self._entry_name(self._redo[-1])
        restore = self._redo_tip if self._redo_tip is not None else self._entry_snap(self._redo[-1])
        self._redo.pop()
        self._sync_history_tip(self._redo, "_redo_tip")
        self._store_history(self._undo, self._history_snapshot(), name, "_undo_tip")
        self._trim_history(self._undo, "_undo_tip")
        self._restore_history(restore)
        return True

    def _restore_history(self, snap: dict[str, Any]) -> None:
        self._restoring = True
        try:
            # The patch shares unchanged dicts with older steps. Copy before
            # the model reads them so a later edit cannot rewrite history.
            self.project = Project.model_validate(copy.deepcopy(snap["project"]))
            aid = str(snap.get("active_sheet_id") or "")
            if aid and any(s.id == aid for s in self.project.sheets):
                self.active_sheet_id = aid
            elif self.project.sheets:
                self.active_sheet_id = self.project.sheets[0].id
            self._wire_seq = int(snap.get("wire_seq") or 0)
            if "path" in snap:
                raw_path = str(snap.get("path") or "")
                self.path = Path(raw_path) if raw_path else None
            catalog = snap.get("catalog")
            if catalog is not None:
                self.catalog = catalog
            self._invalidate_sheet_cache()
        finally:
            self._restoring = False

    def announce(self, model: str = "Grok 4.6", name: str = "Grok", kind: str = "ai") -> dict[str, Any]:
        """Mark this session as driven by an AI so the GUI can show who is working."""
        from panelcad.api.activity import DEFAULT_MODEL

        self.actor = {"kind": kind, "model": model or DEFAULT_MODEL, "name": name or "Grok"}
        return self.emit("hello", f"{self.actor['model']} connected", status="working")

    def set_idle(self, text: str = "idle") -> dict[str, Any]:
        return self.emit("idle", text, status="idle")

    def ui_set_theme(self, theme: str) -> dict[str, Any]:
        """Window theme: light, dark, or system. Stored outside the project."""

        from panelcad.theme import set_theme

        return set_theme(theme)

    def ui_set_canvas(self, canvas: str) -> dict[str, Any]:
        """On-screen sheet: paper, white, dark, or auto. Exports stay white paper."""

        from panelcad.theme import set_canvas

        return set_canvas(canvas)

    def ui_set_phase_colors(self, enabled: bool = True) -> dict[str, Any]:
        """Phase colours on the sheet. Print, PDF, DXF, and SVG stay unchanged."""

        from panelcad.theme import parse_phase_enabled, set_phase_colors

        return set_phase_colors(parse_phase_enabled(enabled))

    def submit_prompt(self, text: str) -> dict[str, Any]:
        """Log a human prompt from the AI dock."""
        from panelcad.api.activity import write_prompt

        raw = " ".join(str(text).split())
        if not raw:
            raise ValueError("empty prompt")
        sheet = self.sheet()
        write_prompt(self.path, raw, sheet=sheet.id, sheet_title=sheet.title)
        return self.emit(
            "prompt",
            f"you: {raw}",
            status="idle",
            extra={"prompt": raw, "actor": {"kind": "human", "model": "", "name": "you"}},
        )

    def run_prompt(self, text: str) -> dict[str, Any]:
        """Do the work described in the prompt line (Session only — no GUI clicks)."""
        from panelcad.api.prompt import run_prompt

        return run_prompt(self, text)

    def emit(
        self,
        op: str,
        summary: str,
        *,
        status: str | None = None,
        sheet: str | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        from panelcad.api.activity import append_jsonl, journal_path, make_event

        sheet_id = sheet or self.active_sheet_id
        title = ""
        kind = ""
        try:
            sh = self.project.sheet(sheet_id) if sheet_id else self.sheet()
            sheet_id = sh.id
            title = sh.title
            kind = sh.kind
        except Exception:  # noqa: BLE001
            pass
        if status:
            st = status
        elif error:
            st = "error"
        elif self.actor.get("kind") == "ai":
            st = "working"
        else:
            st = "human"
        event = make_event(
            op,
            summary,
            sheet=sheet_id,
            sheet_title=title,
            sheet_kind=kind,
            actor=self.actor,
            status=st,
            error=error,
            extra=extra or None,
        )
        self.activity.append(event)
        if len(self.activity) > 300:
            self.activity = self.activity[-300:]
        dest = journal_path(self.path)
        if dest is not None:
            try:
                append_jsonl(dest, event)
            except OSError:
                pass
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass
        if error or st == "error":
            _log_io("error", summary)
        return event

    @classmethod
    def create(cls, name: str, sheet_id: str = "main", title: str | None = None) -> Session:
        sheet = Sheet(id=_slug(sheet_id), title=title or name, kind="schematic", page=Page())
        project = Project(name=name, sheets=[sheet])
        session = cls(project)
        _log_io("create", name)
        _note_project(session)
        return session

    @classmethod
    def open(cls, path: Path | str) -> Session:
        p = absolute_project_path(path) or Path(path)
        project = load_project(p)
        from panelcad.core.ladder import stamp_drawing_style
        from panelcad.reconstruct.store import apply_sidecar

        stamp_drawing_style(project)
        apply_sidecar(project, p)
        session = cls(project, catalog=default_catalog(p.parent), path=p)
        from panelcad.needs_info import install_resolved_devices

        install_resolved_devices(session)
        session._prepare_share()
        _log_io("open", p)
        _bind_primary(session)
        _remember_saved(session)
        _note_project(session)
        return session

    @classmethod
    def open_recovery(
        cls,
        recovery_path: Path | str,
        project_path: Path | str | None = None,
    ) -> Session:
        """Load a recovery copy as unsaved work aimed at the original project.

        File > Save writes ``project_path``. When that path is missing, or it is
        itself inside the recovery folder, Save As chooses a new file. The
        recovery copy is not the save target, and it stays unmodified.
        """

        from panelcad.diag.paths import is_under_recovery

        recovery = Path(recovery_path)
        project = load_project(recovery)
        from panelcad.core.ladder import stamp_drawing_style

        stamp_drawing_style(project)
        target: Path | None = None
        raw = "" if project_path is None else str(project_path).strip()
        if raw and not is_under_recovery(raw):
            target = absolute_project_path(raw)
        session = cls(project, path=target)
        from panelcad.needs_info import install_resolved_devices

        install_resolved_devices(session)
        session.recovered = True
        session._prepare_share()
        _log_io("recover", recovery)
        _bind_primary(session)
        _note_project(session)
        return session

    def sheet(self, sheet_id: str | None = None) -> Sheet:
        return self.project.sheet(sheet_id or self.active_sheet_id or self.project.default_sheet().id)

    def add_sheet(
        self,
        sheet_id: str,
        title: str = "",
        kind: SheetKind = "schematic",
        make_active: bool = True,
    ) -> Sheet:
        sid = _slug(sheet_id)
        if any(s.id == sid for s in self.project.sheets):
            raise ValueError(f"sheet '{sid}' already exists")
        page = Page(size="A3", orientation="portrait") if kind == "panel_layout" else Page()
        sheet = Sheet(id=sid, title=title or sid, kind=kind, page=page)
        if kind == "panel_layout":
            enc, rails = default_enclosure_and_rails(sheet.page)
            sheet.enclosure = enc
            sheet.rails = rails
        if kind == "network":
            sheet.network = NetworkInfo(name=title or sid, medium="mixed")
        if kind == "schematic":
            sheet.drawing_style = self.project.title_block.drawing_style
        if kind in {"panel_layout", "door_layout", "terminal"} and self.project.active_panel:
            from panelcad.core.panels import find_panel

            if find_panel(self.project, self.project.active_panel) is not None:
                sheet.panel = self.project.active_panel
        self.project.sheets.append(sheet)
        if make_active:
            self.active_sheet_id = sid
        return sheet

    def _stamp_device_panel(self, sheet: Sheet, comp: Component, explicit: str | None) -> None:
        """Record which panel a new device belongs to. A single-panel file stays blank."""

        if "offpage" in (comp.library_id or ""):
            return
        from panelcad.core.panels import find_panel

        token = str(explicit or "").strip()
        if not token:
            token = str(sheet.panel or "").strip()
        if not token:
            token = str(self.project.active_panel or "").strip()
        if not token:
            return
        if not self.project.panels:
            raise ValueError(f"no panel '{token}'. Add a panel before assigning a device.")
        panel = find_panel(self.project, token)
        if panel is None:
            raise ValueError(f"no panel '{token}'")
        comp.panel = panel.id

    def list_panels(self) -> list[dict[str, Any]]:
        from panelcad.core.panels import panel_tag

        rows: list[dict[str, Any]] = []
        for panel in self.project.panels:
            sheets = [sheet.id for sheet in self.project.sheets if sheet.panel == panel.id]
            refs: list[str] = []
            for sheet in self.project.sheets:
                for comp in sheet.components:
                    if comp.panel == panel.id and comp.id not in refs:
                        refs.append(comp.id)
            layout = next(
                (sheet for sheet in self.project.sheets if sheet.panel == panel.id and sheet.kind == "panel_layout"),
                None,
            )
            enclosure = None
            if layout is not None and layout.enclosure is not None:
                enc = layout.enclosure
                enclosure = {
                    "catalog": enc.catalog,
                    "manufacturer": enc.manufacturer,
                    "name": enc.name,
                    "width_mm": enc.width_mm,
                    "height_mm": enc.height_mm,
                    "depth_mm": enc.depth_mm,
                    "panel_width_mm": enc.panel_width_mm,
                    "panel_height_mm": enc.panel_height_mm,
                    "sized": bool(enc.sized),
                    "enclosure_id": panel.enclosure_id,
                }
            rows.append(
                {
                    "id": panel.id,
                    "tag": panel_tag(panel),
                    "name": panel.name,
                    "system": panel.system or self.project.location_system,
                    "enclosure_id": panel.enclosure_id,
                    "enclosure": enclosure,
                    "sheets": sheets,
                    "devices": refs,
                    "active": panel.id == self.project.active_panel,
                }
            )
        return rows

    def add_panel(
        self,
        tag: str,
        *,
        name: str = "",
        enclosure_id: str = "",
        system: str = "",
        make_sheets: bool = True,
    ) -> dict[str, Any]:
        """Add a control panel. The first one claims an existing unassigned layout.

        ``enclosure_id`` is an id or catalog number from libraries/enclosures.yaml.
        Millimetres are copied from that row. A free width or height is not accepted.
        """

        label = str(tag or "").strip()
        if not label:
            raise ValueError("panel tag is required")

        def run() -> dict[str, Any]:
            return self._add_panel(
                label,
                name=str(name or "").strip(),
                enclosure_id=str(enclosure_id or "").strip(),
                system=str(system or "").strip(),
                make_sheets=bool(make_sheets),
            )

        return self._undoable("add_panel", f"add panel {label}", run)

    def _add_panel(
        self,
        tag: str,
        *,
        name: str,
        enclosure_id: str,
        system: str,
        make_sheets: bool,
    ) -> dict[str, Any]:
        from panelcad.core.models import Panel
        from panelcad.core.panels import find_panel, panel_tag, require_catalog_box

        if enclosure_id:
            require_catalog_box(enclosure_id, self.path.parent if self.path else None)
        if find_panel(self.project, tag) is not None:
            raise ValueError(f"panel '{tag}' already exists")
        pid = _slug(tag)
        if any(panel.id == pid for panel in self.project.panels):
            raise ValueError(f"panel '{tag}' already exists")
        first = not self.project.panels
        panel = Panel(id=pid, tag=tag, name=name, system=system.lstrip("="))
        self.project.panels.append(panel)
        self.project.active_panel = pid
        back_id = ""
        door_id = ""
        term_id = ""
        if make_sheets:
            back = door = term = None
            if first:
                layouts = [sheet for sheet in self.project.sheets if sheet.kind == "panel_layout" and not sheet.panel]
                doors = [sheet for sheet in self.project.sheets if sheet.kind == "door_layout" and not sheet.panel]
                terms = [sheet for sheet in self.project.sheets if sheet.kind == "terminal" and not sheet.panel]
                if len(layouts) == 1:
                    back = layouts[0]
                    back.panel = pid
                    self._bind_sheet_devices(back, pid, force=False)
                if len(doors) == 1:
                    door = doors[0]
                    door.panel = pid
                    self._bind_sheet_devices(door, pid, force=False)
                if len(terms) == 1:
                    term = terms[0]
                    term.panel = pid
                    self._bind_sheet_devices(term, pid, force=False)
            if first:
                for sheet in self.project.sheets:
                    self._bind_sheet_devices(sheet, pid, force=False)
            if back is None:
                back = self.add_sheet(
                    self._unique_sheet_id(f"{pid}-backpanel"),
                    title=f"{tag} backpanel",
                    kind="panel_layout",
                    make_active=False,
                )
                back.panel = pid
            if door is None:
                door = self.add_sheet(
                    self._unique_sheet_id(f"{pid}-door"),
                    title=f"{tag} door",
                    kind="door_layout",
                    make_active=False,
                )
                door.panel = pid
            if term is None:
                term = self.add_sheet(
                    self._unique_sheet_id(f"{pid}-terminals"),
                    title=f"{tag} terminals",
                    kind="terminal",
                    make_active=False,
                )
                term.panel = pid
                if not term.strip:
                    term.strip = f"{tag}-TB"
            back_id, door_id, term_id = back.id, door.id, term.id
            self.active_sheet_id = back.id
            if back is not None and not enclosure_id:
                self._drop_unsized_backpanel(back)
        if enclosure_id:
            self._apply_panel_enclosure(panel, enclosure_id, replace=False)
        return {
            "id": panel.id,
            "tag": panel_tag(panel),
            "name": panel.name,
            "sheets": {"backpanel": back_id, "door": door_id, "terminals": term_id},
            "sheet": back_id or self.active_sheet_id,
            "enclosure_id": panel.enclosure_id,
            "active": True,
        }

    def _unique_sheet_id(self, base: str) -> str:
        sid = _slug(base)
        if not any(sheet.id == sid for sheet in self.project.sheets):
            return sid
        number = 2
        while any(sheet.id == f"{sid}-{number}" for sheet in self.project.sheets):
            number += 1
        return f"{sid}-{number}"

    def _panel_layout_sheet(self, panel: Any) -> Sheet | None:
        return next(
            (sheet for sheet in self.project.sheets if sheet.panel == panel.id and sheet.kind == "panel_layout"),
            None,
        )

    def set_panel_enclosure(self, panel: str, enclosure_id: str, *, replace: bool = False) -> dict[str, Any]:
        """Write a catalog enclosure onto this panel's backpanel. No free millimetres."""

        from panelcad.core.panels import require_panel

        target = require_panel(self.project, panel)
        box_id = str(enclosure_id or "").strip()

        def run() -> dict[str, Any]:
            return self._apply_panel_enclosure(target, box_id, replace=replace)

        return self._undoable("set_panel_enclosure", f"enclosure {target.tag or target.id}", run)

    def _apply_panel_enclosure(self, panel: Any, enclosure_id: str, *, replace: bool) -> dict[str, Any]:
        from panelcad.core.layout import enclosure_is_sized
        from panelcad.core.panels import fit_backpanel_rails, panel_tag, require_catalog_box

        sheet = self._panel_layout_sheet(panel)
        if sheet is None:
            raise ValueError(f"panel '{panel_tag(panel)}' has no backpanel sheet")
        project_dir = self.path.parent if self.path else None
        box = require_catalog_box(enclosure_id, project_dir)
        enc = sheet.enclosure
        if sheet.components and not replace:
            raise ValueError(
                f"{sheet.id} already has devices. Pass replace=true to write {box.id} over them."
            )
        if (
            not replace
            and enclosure_is_sized(enc, sheet.page)
            and (str(enc.catalog or "").strip() or str(enc.manufacturer or "").strip())
        ):
            raise ValueError(
                f"{sheet.id} already has catalog enclosure {enc.catalog or enc.name}. "
                f"Pass replace=true to write {box.id}."
            )
        self._write_enclosure(
            sheet,
            width_mm=box.width_mm,
            height_mm=box.height_mm,
            depth_mm=box.depth_mm,
            panel_width_mm=box.panel_width_mm,
            panel_height_mm=box.panel_height_mm,
            name=box.catalog or box.id,
            nema_type=box.nema,
            material=box.material,
            catalog=box.catalog,
            manufacturer=box.manufacturer,
            mount=box.mount,
        )
        panel.enclosure_id = box.id
        fit_backpanel_rails(sheet)
        written = sheet.enclosure
        return {
            "panel": panel.id,
            "tag": panel_tag(panel),
            "sheet": sheet.id,
            "enclosure_id": box.id,
            "catalog": box.catalog,
            "manufacturer": box.manufacturer,
            "width_mm": written.width_mm if written else box.width_mm,
            "height_mm": written.height_mm if written else box.height_mm,
            "depth_mm": written.depth_mm if written else box.depth_mm,
            "panel_width_mm": written.panel_width_mm if written else box.panel_width_mm,
            "panel_height_mm": written.panel_height_mm if written else box.panel_height_mm,
        }

    def set_active_panel(self, panel: str) -> dict[str, Any]:
        from panelcad.core.panels import panel_tag, require_panel

        target = require_panel(self.project, panel)
        self.project.active_panel = target.id
        current = self.sheet()
        if current.panel and current.panel != target.id:
            layout = self._panel_layout_sheet(target)
            if layout is not None:
                self.set_active_sheet(layout.id)
        return {
            "id": target.id,
            "tag": panel_tag(target),
            "active_sheet": self.active_sheet_id,
        }

    def assign_device(self, ref: str, panel: str, *, sheet_id: str | None = None) -> dict[str, Any]:
        """Put every copy of this device tag on a panel. The tag itself does not change."""

        from panelcad.core.panels import panel_tag, require_panel

        target = require_panel(self.project, panel)
        want = str(ref or "").strip()
        if not want:
            raise ValueError("ref is required")
        only = str(sheet_id or "").strip()

        def run() -> dict[str, Any]:
            found: list[str] = []
            for sheet in self.project.sheets:
                if only and sheet.id != only:
                    continue
                for comp in sheet.components:
                    if comp.id != want:
                        continue
                    comp.panel = target.id
                    if sheet.id not in found:
                        found.append(sheet.id)
            if not found:
                raise KeyError(f"no component '{want}'")
            return {"ref": want, "panel": target.id, "tag": panel_tag(target), "sheets": found}

        return self._undoable("assign_device", f"assign {want} to {panel_tag(target)}", run)

    def _drop_unsized_backpanel(self, sheet: Sheet) -> None:
        """A new panel does not keep the page-outline box (261×348 on A3)."""

        from panelcad.core.layout import enclosure_is_sized

        if sheet.kind != "panel_layout":
            return
        if enclosure_is_sized(sheet.enclosure, sheet.page):
            return
        sheet.enclosure = None
        if not sheet.components:
            sheet.rails = []
            sheet.trays = []

    def _bind_sheet_devices(self, sheet: Sheet, panel_id: str, *, force: bool) -> list[str]:
        """Put devices on *sheet* onto *panel_id*.

        ``force`` moves devices that already name a panel. Off-page arrows stay put.
        """

        moved: list[str] = []
        for comp in sheet.components:
            if "offpage" in (comp.library_id or ""):
                continue
            if not force and comp.panel:
                continue
            if comp.panel == panel_id:
                continue
            comp.panel = panel_id
            moved.append(comp.id)
        return moved

    def _only_backpanel(self, sheet: Sheet) -> bool:
        if sheet.kind != "panel_layout" or not sheet.panel:
            return False
        others = [
            other
            for other in self.project.sheets
            if other.kind == "panel_layout" and other.panel == sheet.panel and other.id != sheet.id
        ]
        return not others

    def assign_sheet(self, sheet_id: str, panel: str) -> dict[str, Any]:
        from panelcad.core.panels import find_panel, panel_tag, require_panel

        target = require_panel(self.project, panel)
        sheet = self.project.sheet(sheet_id)

        def run() -> dict[str, Any]:
            if sheet.panel != target.id and self._only_backpanel(sheet):
                home = find_panel(self.project, sheet.panel)
                tag = panel_tag(home) if home is not None else sheet.panel
                raise ValueError(
                    f"{sheet.id} is {tag}'s only backpanel. Add another backpanel before moving this sheet."
                )
            sheet.panel = target.id
            force = sheet.kind in {"panel_layout", "door_layout"}
            devices = self._bind_sheet_devices(sheet, target.id, force=force)
            return {
                "sheet": sheet.id,
                "panel": target.id,
                "tag": panel_tag(target),
                "kind": sheet.kind,
                "devices": devices,
            }

        return self._undoable("assign_sheet", f"sheet {sheet.id} on {panel_tag(target)}", run)

    def delete_panel(self, panel: str, *, reassign: str | None = None) -> dict[str, Any]:
        """Remove a panel. Its sheets and devices move to *reassign*, or become unassigned.

        Nothing is deleted except the panel record. Cable ends that named it
        follow the same reassign or clear, so no sheet, device, or cable keeps
        the removed id.
        """

        from panelcad.core.panels import panel_tag, require_panel

        target = require_panel(self.project, panel)
        dest = require_panel(self.project, reassign) if str(reassign or "").strip() else None
        if dest is not None and dest.id == target.id:
            raise ValueError("reassign must be a different panel")

        def run() -> dict[str, Any]:
            new_id = dest.id if dest is not None else ""
            sheets: list[str] = []
            devices: list[str] = []
            for sheet in self.project.sheets:
                if sheet.panel == target.id:
                    sheet.panel = new_id
                    sheets.append(sheet.id)
                for comp in sheet.components:
                    if comp.panel == target.id:
                        comp.panel = new_id
                        devices.append(comp.id)
            for cable in self.project.cables:
                if cable.from_panel == target.id:
                    cable.from_panel = new_id
                if cable.to_panel == target.id:
                    cable.to_panel = new_id
            from panelcad.core.cables import delete_cable

            dropped: list[str] = []
            for cable in list(self.project.cables):
                if cable.kind != "interpanel":
                    continue
                if cable.from_panel and cable.to_panel and cable.from_panel == cable.to_panel:
                    dropped.append(cable.tag)
            for tag in dropped:
                delete_cable(self.project, tag)
            self.project.panels = [item for item in self.project.panels if item.id != target.id]
            if self.project.active_panel == target.id:
                if new_id:
                    self.project.active_panel = new_id
                elif self.project.panels:
                    self.project.active_panel = self.project.panels[0].id
                else:
                    self.project.active_panel = ""
            return {
                "deleted": target.id,
                "tag": panel_tag(target),
                "reassign": panel_tag(dest) if dest is not None else "",
                "sheets": sheets,
                "devices": devices,
                "dropped_cables": dropped,
            }

        label = panel_tag(target)
        summary = f"delete panel {label}" if dest is None else f"delete panel {label} → {panel_tag(dest)}"
        return self._undoable("delete_panel", summary, run)

    def add_interconnect(self, from_panel: str, to_panel: str, **fields: Any) -> dict[str, Any]:
        """A cable from one panel's terminal strip to another's. Not a field cable."""

        from panelcad.core.panels import panel_tag, require_panel

        src = require_panel(self.project, from_panel)
        dst = require_panel(self.project, to_panel)
        if src.id == dst.id:
            raise ValueError("an interconnect needs two different panels")
        payload = dict(fields)
        payload["kind"] = "interpanel"
        payload["from_panel"] = src.id
        payload["to_panel"] = dst.id
        if not str(payload.get("to_device") or "").strip():
            payload["to_device"] = panel_tag(dst)
        if not str(payload.get("to_kind") or "").strip():
            payload["to_kind"] = "panel"
        if not str(payload.get("from_device") or "").strip():
            payload["from_device"] = panel_tag(src)
        return self.add_cable(**payload)

    def interconnect_rows(self) -> list[dict[str, str]]:
        from panelcad.core.panels import interconnect_rows

        return interconnect_rows(self.project)

    @_traced_path("export_interconnect")
    def export_interconnect(self, path: Path | str) -> Path:
        from panelcad.core.panels import INTERCONNECT_COLUMNS
        from panelcad.core.cables import write_rows_csv

        dest = Path(path)
        write_rows_csv(dest, self.interconnect_rows(), INTERCONNECT_COLUMNS)
        return dest

    def set_active_sheet(self, sheet_id: str) -> Sheet:
        raw = str(sheet_id or "").strip()
        want = _slug(raw)
        sheet = None
        for candidate in self.project.sheets:
            if candidate.id == raw or candidate.id == want or candidate.id.lower() == raw.lower():
                sheet = candidate
                break
        if sheet is None:
            sheet = self.project.sheet(raw)
        sheet.closed = False
        self.active_sheet_id = sheet.id
        return sheet

    def set_sheet_title(self, title: str, sheet_id: str | None = None) -> Sheet:
        sheet = self.sheet(sheet_id)
        sheet.title = title
        return sheet

    def open_sheets(self) -> list[Sheet]:
        return [s for s in self.project.sheets if not s.closed]

    def closed_sheets(self) -> list[Sheet]:
        return [s for s in self.project.sheets if s.closed]

    def close_sheet(self, sheet_id: str | None = None) -> dict[str, Any]:
        """Hide a page from the tab bar. The sheet stays in the project."""
        sid = sheet_id or self.active_sheet_id
        sheet = self.project.sheet(sid)
        visible = [s for s in self.project.sheets if not s.closed]
        if len(visible) <= 1 and not sheet.closed:
            raise ValueError("keep at least one page open")
        sheet.closed = True
        if self.active_sheet_id == sid:
            remaining = [s.id for s in self.project.sheets if not s.closed]
            self.active_sheet_id = remaining[0]
        return {"id": sid, "closed": True, "active": self.active_sheet_id}

    def open_sheet(self, sheet_id: str) -> Sheet:
        """Bring a closed page back onto the tab bar and make it active."""
        return self.set_active_sheet(sheet_id)

    def delete_sheet(self, sheet_id: str | None = None) -> None:
        sid = sheet_id or self.active_sheet_id
        sheet = self.project.sheet(sid)
        if self._only_backpanel(sheet):
            from panelcad.core.panels import find_panel, panel_tag

            home = find_panel(self.project, sheet.panel)
            tag = panel_tag(home) if home is not None else sheet.panel
            raise ValueError(
                f"{sheet.id} is {tag}'s only backpanel. Add another backpanel before deleting this sheet."
            )
        if len(self.project.sheets) <= 1:
            raise ValueError("cannot delete the last sheet")
        self.project.sheets = [s for s in self.project.sheets if s.id != sid]
        if self.active_sheet_id == sid:
            visible = [s.id for s in self.project.sheets if not s.closed]
            self.active_sheet_id = visible[0] if visible else self.project.sheets[0].id
            self.project.sheet(self.active_sheet_id).closed = False

    def move_sheet(
        self,
        sheet_id: str,
        *,
        index: int | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        """Move one sheet earlier or later. One undo. Does not rename it."""

        sheet = self.project.sheet(sheet_id)
        old = self.project.sheets.index(sheet)
        if index is not None:
            new = int(index)
        else:
            token = str(direction or "").strip().lower()
            if token in {"earlier", "up", "left", "previous"}:
                new = old - 1
            elif token in {"later", "down", "right", "next"}:
                new = old + 1
            else:
                raise ValueError("move_sheet needs index or direction earlier|later")
        new = max(0, min(len(self.project.sheets) - 1, new))
        if new == old:
            return {"id": sheet.id, "index": old, "moved": False}

        def run() -> dict[str, Any]:
            item = self.project.sheets.pop(old)
            self.project.sheets.insert(new, item)
            return {"id": item.id, "index": new, "from": old, "moved": True}

        return self._undoable("move_sheet", f"move sheet {sheet.id}", run)

    def reorder_sheets(self, ids: list[str]) -> dict[str, Any]:
        """Put sheets in this order. Closed sheets not listed keep their slots. One undo."""

        wanted = [str(item) for item in ids]
        by_id = {sheet.id: sheet for sheet in self.project.sheets}
        missing = [sid for sid in wanted if sid not in by_id]
        if missing:
            raise ValueError(f"unknown sheet '{missing[0]}'")
        if len(wanted) != len(set(wanted)):
            raise ValueError("reorder list has a duplicate sheet")

        def run() -> dict[str, Any]:
            old = list(self.project.sheets)
            if len(wanted) == len(old) and set(wanted) == set(by_id):
                self.project.sheets = [by_id[sid] for sid in wanted]
            else:
                closed_at = [(i, sheet) for i, sheet in enumerate(old) if sheet.closed and sheet.id not in wanted]
                result: list[Any] = [None] * len(old)
                for i, sheet in closed_at:
                    result[i] = sheet
                pending = [by_id[sid] for sid in wanted]
                extra = [sheet for sheet in old if sheet.id not in wanted and sheet not in [row for _i, row in closed_at]]
                queue = pending + extra
                filled: list[Any] = []
                take = 0
                for slot in result:
                    if slot is None:
                        filled.append(queue[take])
                        take += 1
                    else:
                        filled.append(slot)
                filled.extend(queue[take:])
                self.project.sheets = filled
            return {"ids": [sheet.id for sheet in self.project.sheets], "moved": True}

        current = [sheet.id for sheet in self.project.sheets]
        if wanted == current:
            return {"ids": current, "moved": False}
        return self._undoable("reorder_sheets", "reorder sheets", run)

    def drawing_standard(self, sheet_id: str | None = None) -> str:
        from panelcad.core.refs import resolve_drawing_standard

        return resolve_drawing_standard(self, sheet_id)

    def ref_prefix_for(self, library_id: str, sheet_id: str | None = None) -> str:
        """Tag prefix ``place`` will use for this library part on this sheet."""

        from panelcad.core.refs import resolve_ref_prefix

        device = self.catalog.get(library_id)
        return resolve_ref_prefix(
            device,
            standard=self.drawing_standard(sheet_id),
            maps=getattr(self.catalog, "ref_prefix_maps", None),
            overrides=self.project.ref_prefixes,
        )

    def next_ref(self, library_id: str, sheet_id: str | None = None) -> str:
        from panelcad.core.refs import next_tag

        sheet = self.sheet(sheet_id)
        used = {c.id for c in sheet.components}
        return next_tag(self.ref_prefix_for(library_id, sheet.id), used)

    def place(
        self,
        library_id: str,
        ref: str | None = None,
        x: float = 40.0,
        y: float = 40.0,
        rotation: int = 0,
        sheet_id: str | None = None,
        properties: dict[str, Any] | None = None,
        rail_id: str | None = None,
        snap: bool = True,
        scale: float = 1.0,
        from_reconstruct_confirm: bool = False,
        _sanitize: bool = True,
        allow_needs_info: bool = False,
        panel: str | None = None,
    ) -> Component:
        device = self.catalog.get(library_id)
        sheet = self.sheet(sheet_id)
        cid = ref or self.next_ref(library_id, sheet.id)
        if any(c.id == cid for c in sheet.components):
            raise ValueError(f"component '{cid}' already exists on {sheet.id}")
        from panelcad.library.panel import (
            MEASURED_ENVELOPE_LIBRARY_ID,
            NEEDS_INFO_KEY,
            needs_info_library_id,
        )
        from panelcad.reconstruct.placeholder import is_needs_info_component, reconstruct_mode

        incoming = dict(properties or {})
        refuse_nonfinite_sizes(incoming)
        if _sanitize and not from_reconstruct_confirm:
            stored = sanitize_caller_properties(incoming, parts=self.parts)
        else:
            stored = dict(incoming)
        # Every place path, including the sanitized one, drops guessed or
        # photo-derived millimetres on reconstruct.needs_info.
        stored = strip_placeholder_guesses(library_id, stored)
        refuse_nonfinite_sizes(stored)
        if str(stored.get(NEEDS_INFO_KEY) or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        } and not needs_info_library_id(library_id):
            raise LayoutNotReadyError(
                "reconstruct_needs_info is only valid on reconstruct.needs_info — "
                "it cannot mark a library symbol"
            )
        probe = Component(
            id=cid,
            library_id=library_id,
            x=float(x),
            y=float(y),
            properties=stored,
        )
        if is_needs_info_component(probe) and not reconstruct_mode(self) and not allow_needs_info:
            raise LayoutNotReadyError(
                "needs-info placeholders can only be created in reconstruct mode"
            )
        if library_id == MEASURED_ENVELOPE_LIBRARY_ID and not from_reconstruct_confirm:
            raise LayoutNotReadyError(
                "reconstruct.measured_envelope can only be placed by reconstruct_confirm_item"
            )
        if (
            is_layout_sheet(sheet)
            and occupies_panel_layout(device, probe)
            and not is_layout_ready(device, probe, parts=self.parts)
            and not is_needs_info_component(probe)
            and not stored.get("reconstruct_schematic_stub")
        ):
            raise_layout_not_ready(cid, device, probe, parts=self.parts)
        px, py = float(x), float(y)
        if snap and is_layout_sheet(sheet) and effective_footprint(device).din_rail and sheet.rails:
            rail = sheet.rail(rail_id) if rail_id else nearest_rail(sheet, py)
            if rail is not None:
                px, py = place_on_rail(device, rail, px)
        stored_scale = place_scale(sheet, scale)
        comp = Component(
            id=cid,
            library_id=library_id,
            x=px,
            y=py,
            rotation=rotation,  # type: ignore[arg-type]
            properties=stored,
            scale=stored_scale,
        )
        sheet.components.append(comp)
        self._stamp_device_panel(sheet, comp, panel)
        return comp

    def place_from_bom(
        self,
        library_id: str,
        ref: str | None = None,
        x: float = 40.0,
        y: float = 40.0,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Put a library part on the active sheet from the BOM.

        The catalog entry is used once — never cloned. Same ref on another
        sheet is the same bill item (schematic vs ladder vs network vs panel).
        """

        device = self.catalog.get(library_id)
        sheet = self.sheet(sheet_id)
        cid = str(ref or "").strip()
        source: Component | None = None
        if cid:
            for sh in self.project.sheets:
                if any(c.id == cid for c in sh.components) and sh.id == sheet.id:
                    existing = sheet.component(cid)
                    return {
                        "id": existing.id,
                        "library_id": existing.library_id,
                        "sheet": sheet.id,
                        "kind": sheet.kind,
                        "placed": False,
                        "reason": "already_on_sheet",
                        "x": existing.x,
                        "y": existing.y,
                    }
            for sh in self.project.sheets:
                try:
                    source = sh.component(cid)
                    library_id = source.library_id
                    device = self.catalog.get(library_id)
                    break
                except KeyError:
                    continue
            else:
                source = None
        else:
            cid = self.next_ref(library_id, sheet.id)

        px, py = self._xy_for_sheet_kind(sheet, device, float(x), float(y))
        props = dict(source.properties) if source is not None else {}
        snap = is_layout_sheet(sheet)
        if snap:
            ghost = source or Component(id=cid, library_id=library_id, x=px, y=py, properties=props)
            if occupies_panel_layout(device, ghost) and not is_layout_ready(
                device, ghost, parts=self.parts
            ):
                raise_layout_not_ready(cid, device, ghost, parts=self.parts)
        if any(c.id == cid for c in sheet.components):
            existing = sheet.component(cid)
            return {
                "id": existing.id,
                "library_id": existing.library_id,
                "sheet": sheet.id,
                "kind": sheet.kind,
                "placed": False,
                "reason": "already_on_sheet",
                "x": existing.x,
                "y": existing.y,
            }
        if source is not None:
            from panelcad.library.panel import should_skip_layout_occupant
            from panelcad.reconstruct.placeholder import is_needs_info_component, reconstruct_mode

            ghost = Component(
                id=cid,
                library_id=library_id,
                x=px,
                y=py,
                properties=props,
                ports=list(source.ports),
                scale=place_scale(sheet, source.scale),
                picture=source.picture,
                pin_overrides=dict(source.pin_overrides),
            )
            if is_needs_info_component(ghost) and not reconstruct_mode(self):
                return {
                    "id": cid,
                    "library_id": library_id,
                    "sheet": sheet.id,
                    "kind": sheet.kind,
                    "placed": False,
                    "reason": "not_layout_ready",
                    "message": "needs-info placeholders can only be created in reconstruct mode",
                    "x": px,
                    "y": py,
                }
            if (
                is_layout_sheet(sheet)
                and not is_needs_info_component(ghost)
                and should_skip_layout_occupant(self.project, device, ghost, sheet=sheet.id)
            ):
                return {
                    "id": cid,
                    "library_id": library_id,
                    "sheet": sheet.id,
                    "kind": sheet.kind,
                    "placed": False,
                    "reason": "not_layout_ready",
                    "message": layout_ready_error(cid, device, ghost),
                    "x": px,
                    "y": py,
                }
            comp = ghost
            if snap and effective_footprint(device, source).din_rail and sheet.rails:
                rail = nearest_rail(sheet, py)
                if rail is not None:
                    comp.x, comp.y = place_on_rail(device, rail, px)
            sheet.components.append(comp)
            if not comp.panel:
                self._stamp_device_panel(sheet, comp, None)
        else:
            comp = self.place(
                library_id, ref=cid, x=px, y=py, sheet_id=sheet.id, properties=props, snap=snap
            )
        return {
            "id": comp.id,
            "library_id": comp.library_id,
            "sheet": sheet.id,
            "kind": sheet.kind,
            "placed": True,
            "x": comp.x,
            "y": comp.y,
        }

    def _xy_for_sheet_kind(
        self, sheet: Sheet, device: Any, x: float, y: float
    ) -> tuple[float, float]:
        """Schematic / ladder / I/O / network / panel each land the same library part differently."""

        from panelcad.core.ladder import is_ladder_sheet, series_rung_ys
        from panelcad.render.io_ladder import TOP_Y, is_io_module, io_card_column

        if is_layout_sheet(sheet):
            return x, y
        if is_network_sheet(sheet):
            return round(x / 8.0) * 8.0, round(y / 8.0) * 8.0
        if is_io_sheet(sheet):
            if is_io_module(device):
                return io_card_column(device), max(TOP_Y, y)
            return x, y
        if is_ladder_sheet(sheet, self.project):
            pins = [p for p in device.pins if p.side in ("left", "right")]
            off = pins[0].y if pins else device.schematic_height / 2.0
            ys = series_rung_ys(sheet, self.catalog)
            wx, wy, _ww, _wh = working_rect(sheet)
            rung = min(ys, key=lambda yy: abs(yy - (y + off))) if ys else wy + 42.0
            return x, rung - off
        return x, y

    def wire(
        self,
        frm: str,
        to: str,
        net: str | None = None,
        sheet_id: str | None = None,
        wire_id: str | None = None,
        label: str | None = None,
        to_sheet: str | None = None,
        number: str | None = None,
        color: str | None = None,
        gauge: str | None = None,
        cable: str | None = None,
        wire_type: str | None = None,
        from_at: Any = None,
        to_at: Any = None,
    ) -> Wire | dict[str, Any]:
        src = self.sheet(sheet_id)
        dest_id = str(to_sheet or "").strip()
        if dest_id and dest_id != src.id:
            return self.wire_offpage(frm, to, net=net, from_sheet=src.id, to_sheet=dest_id, label=label)
        self._assert_pin(src, frm)
        self._assert_pin(src, to)
        if str(frm).strip().upper() == str(to).strip().upper():
            raise ValueError(f"wire from and to are the same pin ({frm})")
        from panelcad.core.circuit import control_power_conflict
        from panelcad.core.ladder import rail_short_message
        from panelcad.render.sheet import parse_pin_ref

        short = rail_short_message(src, frm, to, self.project)
        if short:
            raise ValueError(short)
        # Two device pins are always checked. A bare rail name has no pin, so
        # L1.X2 is the device and a bare L1 is not.
        if "." in str(frm) and "." in str(to):
            a_ref, a_pin = parse_pin_ref(frm)
            b_ref, b_pin = parse_pin_ref(to)
            mix = control_power_conflict(
                src.component(a_ref).library_id,
                a_pin,
                src.component(b_ref).library_id,
                b_pin,
            )
            if mix:
                raise ValueError(mix)
        self._wire_seq += 1
        wid = wire_id or f"w{self._wire_seq}"
        while any(w.id == wid for w in src.wires):
            self._wire_seq += 1
            wid = f"w{self._wire_seq}"
        cap = (label or net or "").strip()
        item = Wire.model_validate(
            {"id": wid, "from": frm, "to": to, "net": net or cap or None, "label": cap, "points": []}
        )
        item.from_at = _rail_click(src, self.project, self.catalog, frm, to, from_at)
        item.to_at = _rail_click(src, self.project, self.catalog, to, frm, to_at)
        if number:
            item.number = str(number).strip()
            item.number_locked = True
        if color:
            item.color = str(color).strip()
        if gauge:
            item.gauge = str(gauge).strip()
        if cable:
            item.cable = str(cable).strip()
        if wire_type:
            item.wire_type = str(wire_type).strip()
        src.wires.append(item)
        from panelcad.core.cleanup import orient_two_pin_flow
        from panelcad.render.sheet import parse_pin_ref

        for end in (frm, to):
            try:
                cid, _ = parse_pin_ref(end)
            except ValueError:
                continue
            orient_two_pin_flow(src, self.catalog, cid)
        self._auto_number_wires()
        return item

    def wire_offpage(
        self,
        frm: str,
        to: str,
        *,
        net: str | None = None,
        from_sheet: str | None = None,
        to_sheet: str,
        label: str | None = None,
        src_rung_y: float | None = None,
        dst_rung_y: float | None = None,
    ) -> dict[str, Any]:
        """Wire a pin on this page to a pin on another page via matching TO / FROM arrows."""
        from panelcad.core.cleanup import sheet_number
        from panelcad.core.layout import is_io_sheet
        from panelcad.render.io_ladder import LEFT_X, RIGHT_X, io_endpoint, io_pin_is_input, is_io_module
        from panelcad.render.sheet import parse_pin_ref, pin_world

        src = self.sheet(from_sheet)
        dest = self.project.sheet(to_sheet)
        self._assert_pin(src, frm)
        self._assert_pin(dest, to)
        src_cid, src_pid = parse_pin_ref(frm)
        dst_cid, dst_pid = parse_pin_ref(to)
        cap = (label or net or src_cid).strip()
        link = f"{src.id}:{frm}|{dest.id}:{to}"
        src_no = sheet_number(self.project.sheets, src.id)
        dst_no = sheet_number(self.project.sheets, dest.id)

        leaving_right = True
        try:
            sdev = self.catalog.get(src.component(src_cid).library_id)
            flag = io_pin_is_input(sdev, src_pid) if is_io_module(sdev) else None
            if flag is True:
                leaving_right = False
        except Exception:  # noqa: BLE001
            pass

        def _xy(sheet: Sheet, ref: str) -> tuple[float, float]:
            if is_io_sheet(sheet):
                return io_endpoint(sheet, self.catalog, ref)
            cid, pid = parse_pin_ref(ref)
            comp = sheet.component(cid)
            return pin_world(comp, self.catalog.get(comp.library_id).pin(pid))

        _ax, ay = _xy(src, frm)
        _dx, dy = _xy(dest, to)

        from panelcad.core.panels import cross_panel_tag

        src_comp = src.component(src_cid)
        dst_comp = dest.component(dst_cid)
        to_panel = cross_panel_tag(self.project, src, src_comp, dest, dst_comp)
        from_panel = cross_panel_tag(self.project, dest, dst_comp, src, src_comp)
        to_callout = f"TO {to_panel} {dst_no} / {dst_cid}" if to_panel else f"TO {dst_no} / {dst_cid}"
        from_callout = f"FROM {from_panel} {src_no} / {src_cid}" if from_panel else f"FROM {src_no} / {src_cid}"

        op_src_id = self.next_ref("iec.connection.offpage", src.id)
        op_dst_id = self.next_ref("iec.connection.offpage_from", dest.id)
        src_props = {
            "tag": cap,
            "callout": to_callout,
            "dest_sheet": dest.id,
            "dest_title": dest.title,
            "dest_ref": dst_cid,
            "dest_pin": dst_pid,
            "link": link,
            "role": "to",
        }
        dst_props = {
            "tag": cap,
            "callout": from_callout,
            "dest_sheet": src.id,
            "dest_title": src.title,
            "dest_ref": src_cid,
            "dest_pin": src_pid,
            "link": link,
            "role": "from",
        }
        if is_io_sheet(src) or is_io_sheet(dest):
            src_x = RIGHT_X if leaving_right else LEFT_X - 4.0
            dst_x = LEFT_X if leaving_right else RIGHT_X
            self.place(
                "iec.connection.offpage",
                ref=op_src_id,
                x=src_x,
                y=ay - 6.0,
                sheet_id=src.id,
                properties=src_props,
                snap=False,
            )
            self.place(
                "iec.connection.offpage_from",
                ref=op_dst_id,
                x=dst_x,
                y=dy - 6.0,
                sheet_id=dest.id,
                properties=dst_props,
                snap=False,
            )
        else:
            from panelcad.core.circuit_ladder import seat_offpage_arrow

            seat_offpage_arrow(
                self,
                src.id,
                "iec.connection.offpage",
                op_src_id,
                rung_y=float(ay if src_rung_y is None else src_rung_y),
                side="right" if leaving_right else "left",
                properties=src_props,
            )
            seat_offpage_arrow(
                self,
                dest.id,
                "iec.connection.offpage_from",
                op_dst_id,
                rung_y=float(dy if dst_rung_y is None else dst_rung_y),
                side="left" if leaving_right else "right",
                properties=dst_props,
            )
        w1 = self.wire(frm, f"{op_src_id}.A", net=cap, sheet_id=src.id, label=cap)
        w2 = self.wire(f"{op_dst_id}.A", to, net=cap, sheet_id=dest.id, label=cap)
        return {
            "from": frm,
            "to": to,
            "to_sheet": dest.id,
            "net": cap,
            "from_op": op_src_id,
            "to_op": op_dst_id,
            "from_wire": w1.id,
            "to_wire": w2.id,
            "link": link,
            "callout": to_callout,
        }

    def insert_circuit(
        self,
        kind: str = "dol",
        drive: str | None = None,
        branch: str = "breaker",
        line_contactor: bool = False,
        control: str = "none",
        kw: float | None = None,
        hp: float | None = None,
        sheet_id: str | None = None,
        new_sheet: bool = False,
        x: float = 40,
        y: float = 22,
        voltage: float | None = None,
        control_voltage: float | None = None,
        fla: float | None = None,
        pilot_lights: bool = False,
        bypass: bool = False,
        control_wiring: str = "3wire",
        starter: str = "dol",
        timer_seconds: float | None = None,
        overload_placement: str = "windings",
        hoa: bool = True,
        plc_model: str | None = None,
        io_run: str | None = None,
        io_ol: str | None = None,
        io_auto: str | None = None,
        io_out: str | None = None,
        manufacturer: str | None = None,
        secondary_manufacturer: str | None = None,
        phases: int | None = None,
        place_layout: bool = True,
        shared_supply: bool = True,
        shared_estop: bool = True,
        separate_control_rail: bool = False,
        standard: str | None = None,
    ) -> dict[str, Any]:
        """Insert a motor circuit on a schematic sheet.

        Kinds: ``dol``, ``vfd``, ``reversing``, ``star_delta``, ``soft_starter``,
        ``plc_motor``. Symbols follow the sheet ``standard`` when set, otherwise
        the title-block standard (default NEMA/ANSI). An explicit IEC project
        keeps ``iec.*``. New in-cabinet devices are added to the existing panel
        (or a new Enclosure). Local operators go on a door_layout sheet.
        Reversing / star-delta / soft starter / PLC put control on a Motor
        control sheet.

        One checkpoint, so one undo removes the whole circuit (and the panel
        pack). If the builder raises, that checkpoint is undone and the sheet
        is left as it was. Does not replace the project.
        """

        from panelcad.core.circuit_builder import build_motor_circuit, check_circuit_args, refuse_non_three_phase

        refuse_non_three_phase(phases)
        check_circuit_args(
            kind,
            drive,
            branch,
            control,
            control_wiring,
            starter,
            overload_placement=overload_placement,
            plc_model=plc_model,
        )
        if self._restoring:
            raise RuntimeError("cannot insert a circuit while undoing")
        self.begin_command("insert circuit")
        failed = True
        try:
            sizing = dict(self.project.properties.get("enclosure_sizing") or {})
            flag = separate_control_rail
            if isinstance(flag, str):
                flag = flag.strip().lower() in {"1", "true", "yes", "y", "on"}
            sizing["separate_control_rail"] = bool(flag)
            self.project.properties["enclosure_sizing"] = sizing
            result = build_motor_circuit(
                self,
                kind=kind,
                drive=drive,
                branch=branch,
                line_contactor=line_contactor,
                control=control,
                kw=kw,
                hp=hp,
                sheet_id=sheet_id,
                new_sheet=new_sheet,
                x=x,
                y=y,
                voltage=voltage,
                control_voltage=control_voltage,
                fla=fla,
                pilot_lights=pilot_lights,
                bypass=bypass,
                control_wiring=control_wiring,
                starter=starter,
                timer_seconds=timer_seconds,
                overload_placement=overload_placement,
                hoa=hoa,
                plc_model=plc_model,
                io_run=io_run,
                io_ol=io_ol,
                io_auto=io_auto,
                io_out=io_out,
                place_layout=place_layout,
                shared_supply=shared_supply,
                shared_estop=shared_estop,
                standard=standard,
            )
            if manufacturer:
                from panelcad.library.preferred import apply_preferred_parts

                result["preferred"] = apply_preferred_parts(
                    self,
                    result,
                    manufacturer,
                    voltage=voltage,
                    control_voltage=control_voltage,
                    fla=fla,
                    secondary_manufacturer=secondary_manufacturer,
                )
                layout_sheet = (result.get("layout") or {}).get("sheet")
                if layout_sheet:
                    from panelcad.core.layout import settle_catalog_footprints

                    notes = settle_catalog_footprints(self, layout_sheet)
                    if notes:
                        result["layout"].setdefault("warnings", []).extend(notes)
                from panelcad.core.ga500 import apply_ga500_if_assigned

                apply_ga500_if_assigned(self, result)
            failed = False
        finally:
            if failed:
                self._cancel_command()
            else:
                self.end_command()
        try:
            self.emit(
                "insert_circuit",
                f"insert {result.get('kind')} circuit on {result.get('sheet')}",
            )
        except Exception:  # noqa: BLE001
            pass
        self._auto_number_wires()
        return result

    def cleanup(self, sheet_id: str | None = None) -> dict[str, Any]:
        """Unstack and spread schematic symbols, fit I/O to rungs, keep them on the page."""
        from panelcad.core.cleanup import cleanup_project

        return cleanup_project(self, sheet_id)

    def page_density(self, sheet_id: str | None = None) -> dict[str, Any]:
        """Devices and rungs against the usable-area limit. Does not write."""

        from panelcad.core.spacing import project_density

        return project_density(self.project, self.catalog, sheet_id)

    def split_sheet(self, sheet_id: str | None = None, apply: bool = False) -> dict[str, Any]:
        """Recommend a continuation page, or move the overflow when apply is true.

        Crossing conductors become off-page references. The limit is the usable
        area of the sheet, not a fixed device count.
        """

        from panelcad.core.spacing import split_plan

        sheet = self.sheet(sheet_id)
        plan = split_plan(sheet, self.catalog)
        base = sheet.id
        continuation = f"{base}-2"
        n = 2
        while any(other.id == continuation for other in self.project.sheets):
            n += 1
            continuation = f"{base}-{n}"
        plan["continuation"] = continuation if plan.get("move") else ""
        plan["applied"] = False
        if not apply or not plan.get("move"):
            return plan

        def _run() -> dict[str, Any]:
            from panelcad.core.spacing import page_fullness, split_plan as plan_for

            pages = [sheet.id]
            links: list[dict[str, Any]] = []
            guard = 0
            while guard < 8:
                guard += 1
                progressed = False
                for sid in list(pages):
                    page = self.sheet(sid)
                    if not page_fullness(page, self.catalog).get("over_dense"):
                        continue
                    step = plan_for(page, self.catalog)
                    if not step.get("move"):
                        continue
                    base = page.id
                    continuation = f"{base}-2"
                    n = 2
                    while any(other.id == continuation for other in self.project.sheets):
                        n += 1
                        continuation = f"{base}-{n}"
                    step["continuation"] = continuation
                    result = self._apply_split(page.id, step)
                    if result.get("continuation") and result["continuation"] not in pages:
                        pages.append(result["continuation"])
                    links.extend(result.get("offpage") or [])
                    progressed = True
                if not progressed:
                    break
            from panelcad.core.spacing import page_fullness as fullness

            return {
                "applied": True,
                "sheet": sheet.id,
                "pages": pages,
                "continuation": pages[-1] if len(pages) > 1 else "",
                "offpage": links,
                "density": [fullness(self.sheet(sid), self.catalog) for sid in pages],
                "over_dense": [
                    sid for sid in pages if fullness(self.sheet(sid), self.catalog).get("over_dense")
                ],
            }

        return self._undoable("split_sheet", f"split {sheet.id}", _run)

    def _apply_split(self, sheet_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        from panelcad.render.titleblock import working_rect

        sheet = self.sheet(sheet_id)
        move_ids = set(plan.get("move") or [])
        move_ids |= self._split_attached_arrows(sheet, move_ids)
        moving = [comp for comp in sheet.components if comp.id in move_ids]
        if not moving:
            plan["applied"] = False
            return plan
        title = f"{sheet.title or sheet.id} (cont.)"
        dest = self.add_sheet(str(plan["continuation"]), title=title, kind=sheet.kind, make_active=False)
        dest.drawing_style = sheet.drawing_style
        dest.rail_hot = sheet.rail_hot
        dest.rail_return = sheet.rail_return
        dest.standard = sheet.standard
        dest.description = f"Continuation of {sheet.id}"
        # A continuation stays on its source sheet's panel (multi-panel projects).
        dest.panel = sheet.panel

        def _owner(end: str) -> str | None:
            ref = str(end).split(".", 1)[0]
            if any(comp.id == ref for comp in sheet.components) or ref in move_ids:
                return ref
            return None

        stay_wires = []
        moved_wires = []
        crossing: list[tuple[str, str, str, str]] = []
        for wire in list(sheet.wires):
            ends = (wire.frm, wire.to)
            owners = [_owner(end) for end in ends]
            device_owners = [owner for owner in owners if owner]
            rail_ends = [end for end, owner in zip(ends, owners, strict=True) if owner is None]
            net = str(wire.net or wire.number or "")
            # A moved pin landed on a shared rail. Reference that rail; do not
            # copy it onto the continuation as if it were a new source.
            if rail_ends and device_owners and all(owner in move_ids for owner in device_owners):
                device_end = next(end for end, owner in zip(ends, owners, strict=True) if owner in move_ids)
                crossing.append((device_end, rail_ends[0], net, "rail"))
                continue
            owner_set = set(device_owners)
            if not owner_set or owner_set.isdisjoint(move_ids):
                stay_wires.append(wire)
                continue
            if owner_set <= move_ids:
                moved_wires.append(wire)
                continue
            keep_end = wire.frm if _owner(wire.frm) not in move_ids else wire.to
            move_end = wire.to if keep_end == wire.frm else wire.frm
            crossing.append((keep_end, move_end, net, "device"))
        sheet.wires = stay_wires
        sheet.components = [comp for comp in sheet.components if comp.id not in move_ids]
        _wx, wy, _ww, _wh = working_rect(dest)
        top = min(float(comp.y) for comp in moving)
        dy = (wy + 18.0) - top
        for comp in moving:
            comp.y = float(comp.y) + dy
            dest.components.append(comp)
        for wire in moved_wires:
            wire.points = []
            dest.wires.append(wire)
        self._repoint_moved_references(sheet.id, dest, move_ids)
        from panelcad.core.circuit_ladder import link_split_feeds

        links = link_split_feeds(self, sheet, dest, crossing)
        from panelcad.core.spacing import page_fullness

        plan = dict(plan)
        plan["applied"] = True
        plan["continuation"] = dest.id
        plan["offpage"] = links
        plan["density"] = [
            page_fullness(sheet, self.catalog),
            page_fullness(dest, self.catalog),
        ]
        return plan

    def _split_attached_arrows(self, sheet: Any, move_ids: set[str]) -> set[str]:
        """Off-page arrows wired only to devices that are moving go with them.

        Leaving the arrow behind turns its one wire into a relay pair and the
        far page then names a device that is no longer on this sheet.
        """

        from panelcad.core.xref import _device, is_offpage_component

        extra: set[str] = set()
        for comp in sheet.components:
            if comp.id in move_ids or not is_offpage_component(comp, _device(self.catalog, comp)):
                continue
            others: list[str] = []
            for wire in sheet.wires:
                ends = [str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0]]
                if comp.id not in ends:
                    continue
                ends.remove(comp.id)
                others.append(ends[0])
            if others and all(ref in move_ids for ref in others):
                extra.add(comp.id)
        return extra

    def _repoint_moved_references(self, source_id: str, dest: Any, move_ids: set[str]) -> None:
        """Off-page references to a moved device follow it to the continuation."""

        from panelcad.core.xref import _device, is_offpage_component, offpage_callout

        def _end(text: str) -> str:
            sid, sep, rest = text.partition(":")
            if sep and sid == source_id and not rest.startswith("rail:"):
                if rest.split(".", 1)[0] in move_ids:
                    return f"{dest.id}:{rest}"
            return text

        touched: list[tuple[Any, Any]] = []
        for page in self.project.sheets:
            for comp in page.components:
                if not is_offpage_component(comp, _device(self.catalog, comp)):
                    continue
                props = comp.properties
                changed = False
                link = str(props.get("link") or "")
                if "|" in link:
                    relinked = "|".join(_end(part) for part in link.split("|"))
                    if relinked != link:
                        props["link"] = relinked
                        changed = True
                if str(props.get("dest_sheet") or "") == source_id:
                    ref = str(props.get("dest_ref") or "") or str(props.get("dest_pin") or "").split(".", 1)[0]
                    if ref in move_ids:
                        props["dest_sheet"] = dest.id
                        if "dest_title" in props:
                            props["dest_title"] = dest.title
                        changed = True
                if changed:
                    touched.append((page, comp))
        for page, comp in touched:
            if "callout" in comp.properties:
                text = offpage_callout(self.project, self.catalog, page, comp)
                if text:
                    comp.properties["callout"] = text

    def delete_component(self, ref: str, sheet_id: str | None = None) -> None:
        sheet = self.sheet(sheet_id)
        self._release_io_field(sheet, ref)
        sheet.components = [c for c in sheet.components if c.id != ref]
        sheet.wires = [
            w for w in sheet.wires if not (w.frm.startswith(ref + ".") or w.to.startswith(ref + "."))
        ]
        self._auto_number_wires()

    def _release_io_field(self, sheet: Any, ref: str) -> None:
        """Deleting a field device marks that I/O point spare so regenerate does not redraw it."""

        try:
            comp = sheet.component(ref)
        except KeyError:
            return
        link = comp.properties.get("io_point")
        if not isinstance(link, dict) or str(link.get("role") or "") == "terminal":
            return
        module_ref = str(link.get("module") or "")
        pin = str(link.get("pin") or "")
        if not module_ref or not pin:
            return
        for page in self.project.sheets:
            try:
                module = page.component(module_ref)
            except KeyError:
                continue
            if not module.properties.get("io_module"):
                continue
            io_map = module.properties.get("io")
            if not isinstance(io_map, dict) or not isinstance(io_map.get(pin), dict):
                return
            meta = dict(io_map[pin])
            terminal = str(meta.get("terminal") or pin)
            safe = "".join(ch if ch.isalnum() else "_" for ch in terminal)
            meta["field"] = "spare"
            meta["user_removed"] = True
            meta["field_ref"] = ""
            meta["field_library"] = ""
            meta["user_edited"] = True
            meta["tag"] = f"SPARE_{module_ref}_{safe}"
            meta["description"] = meta.get("description") or "spare"
            io_map[pin] = meta
            module.properties["io"] = io_map
            return

    def rename_component(self, old: str, new: str, sheet_id: str | None = None) -> dict[str, str]:
        """Rename a device. A field tag is copied back onto the I/O list."""

        from panelcad.core.electrical import rename_component as rename
        from panelcad.core.io_sheets import pull_io_sheet_edits

        def run() -> dict[str, str]:
            page = self.sheet(sheet_id)
            rename(self.project, page, old, new)
            pull_io_sheet_edits(self.project, self.catalog)
            return {"ref": new, "old": old, "sheet": page.id}

        return self._undoable("rename_component", f"rename {old} to {new}", run)

    def duplicate_component(self, ref: str, sheet_id: str | None = None) -> Component:
        """Place a copy of *ref* offset 10 mm, including ports, scale, and properties."""
        sheet = self.sheet(sheet_id)
        src = sheet.component(ref)
        props = sanitize_caller_properties(dict(src.properties), parts=self.parts)
        device = self.catalog.get(src.library_id)
        if assigned_catalog_ready(src, parts=self.parts, device=device):
            for key in ("catalog_key", "catalog_verified", "catalog_source"):
                if src.properties.get(key) not in (None, ""):
                    props[key] = src.properties[key]
            if str(src.properties.get("dim_source") or "") == "catalog":
                props["dim_source"] = "catalog"
        elif envelope_is_confirmed(src):
            props[ENVELOPE_CONFIRMED_KEY] = src.properties.get(ENVELOPE_CONFIRMED_KEY)
            props[ENVELOPE_BY_KEY] = src.properties.get(ENVELOPE_BY_KEY, "")
            props[ENVELOPE_AT_KEY] = src.properties.get(ENVELOPE_AT_KEY, "")
            for key in ENVELOPE_SNAPSHOT_KEYS:
                if src.properties.get(key) not in (None, ""):
                    props[key] = src.properties[key]
        dest = self.place(
            src.library_id,
            x=src.x + 10.0,
            y=src.y + 10.0,
            rotation=int(src.rotation),
            properties=props,
            sheet_id=sheet.id,
            snap=False,
            _sanitize=False,
            from_reconstruct_confirm=src.library_id == "reconstruct.measured_envelope",
        )
        dest.scale = src.scale
        dest.mirror = src.mirror
        dest.ports = [p.model_copy(deep=True) for p in src.ports]
        dest.pins_locked = src.pins_locked
        dest.pin_overrides = {k: v.model_copy() for k, v in src.pin_overrides.items()}
        dest.picture = src.picture
        return dest

    def rotate_component(self, ref: str, degrees: int = 90, sheet_id: str | None = None) -> Component:
        """Rotate a schematic/network device in 90° steps (clockwise)."""
        comp = self.sheet(sheet_id).component(ref)
        step = int(degrees)
        if step % 90 != 0:
            raise ValueError("rotation must be a multiple of 90")
        new = (int(comp.rotation) + step) % 360
        if new not in (0, 90, 180, 270):
            new = 0
        comp.rotation = new  # type: ignore[assignment]
        return comp

    def delete_wire(self, wire_id: str, sheet_id: str | None = None) -> None:
        sheet = self.sheet(sheet_id)
        before = len(sheet.wires)
        sheet.wires = [w for w in sheet.wires if w.id != wire_id]
        if len(sheet.wires) == before:
            raise KeyError(f"no wire '{wire_id}' on sheet '{sheet.id}'")
        self._auto_number_wires()

    def find_wire(
        self,
        wire_id: str | None = None,
        frm: str | None = None,
        to: str | None = None,
        sheet_id: str | None = None,
    ) -> Wire:
        sheet = self.sheet(sheet_id)
        if wire_id:
            for w in sheet.wires:
                if w.id == wire_id:
                    return w
            raise KeyError(f"no wire '{wire_id}' on sheet '{sheet.id}'")
        if frm and to:
            for w in sheet.wires:
                if {w.frm, w.to} == {frm, to}:
                    return w
            raise KeyError(f"no wire {frm} → {to} on sheet '{sheet.id}'")
        raise ValueError("pass wire_id or from/to")

    def set_wire_route(
        self,
        wire_id: str,
        points: list[Any],
        sheet_id: str | None = None,
    ) -> Wire:
        """Store interior waypoints so a human-pulled wire keeps its shape."""

        from panelcad.core.geometry import Point
        from panelcad.render.wiring import clear_route_cache

        wire = self.find_wire(wire_id=wire_id, sheet_id=sheet_id)
        out: list[Point] = []
        for raw in points or []:
            if isinstance(raw, Point):
                out.append(Point(x=float(raw.x), y=float(raw.y)))
            elif isinstance(raw, dict):
                out.append(Point(x=float(raw.get("x", 0)), y=float(raw.get("y", 0))))
            else:
                out.append(Point(x=float(raw[0]), y=float(raw[1])))
        from panelcad.core.route import orthogonal_interior
        from panelcad.render.wiring import wire_ends

        ax, ay, _a_side, bx, by, _b_side = wire_ends(
            self.sheet(sheet_id), self.catalog, wire, self.project
        )
        wire.points = orthogonal_interior(ax, ay, bx, by, out)
        clear_route_cache()
        self._touch_drawing()
        self.emit("set_wire_route", f"route {wire.id}")
        return wire

    def drop_wire_routes(
        self,
        refs: list[str] | set[str] | None = None,
        sheet_id: str | None = None,
    ) -> int:
        """Forget stored waypoints on wires that touch *refs* (or every wire).

        Call this after a device moves so the autorouter can run again.
        A clicked rail landing stays. Reset route is what clears that.
        """

        from panelcad.render.wiring import clear_route_cache

        sheet = self.sheet(sheet_id)
        if refs is None:
            want = None
        else:
            want = {str(r) for r in refs}
        n = 0
        for wire in sheet.wires:
            if want is not None:
                a = str(wire.frm).split(".", 1)[0]
                b = str(wire.to).split(".", 1)[0]
                if a not in want and b not in want:
                    continue
            if wire.points:
                wire.points = []
                n += 1
        clear_route_cache()
        if n:
            self._touch_drawing()
        return n

    def clear_wire_route(self, wire_id: str, sheet_id: str | None = None) -> Wire:
        from panelcad.render.wiring import clear_route_cache

        wire = self.find_wire(wire_id=wire_id, sheet_id=sheet_id)
        wire.points = []
        wire.from_at = None
        wire.to_at = None
        clear_route_cache()
        self._touch_drawing()
        self.emit("clear_wire_route", f"auto-route {wire.id}")
        return wire

    def set_wire_label(
        self,
        wire_id: str | None = None,
        label: str | None = None,
        net: str | None = None,
        frm: str | None = None,
        to: str | None = None,
        sheet_id: str | None = None,
    ) -> Wire:
        """Set the text drawn on a wire. `label` is shown; `net` is the electrical name."""
        wire = self.find_wire(wire_id=wire_id, frm=frm, to=to, sheet_id=sheet_id)
        if label is not None:
            wire.label = str(label).strip()
            if not wire.net:
                wire.net = wire.label or None
        if net is not None:
            wire.net = str(net).strip() or None
        return wire

    def set_wire_spec(
        self,
        wire_id: str | None = None,
        *,
        frm: str | None = None,
        to: str | None = None,
        sheet_id: str | None = None,
        number: str | None = None,
        color: str | None = None,
        gauge: str | None = None,
        cable: str | None = None,
        locked: bool | None = None,
        wire_type: str | None = None,
    ) -> Wire:
        """Wire number, color, gauge, type, and cable marker. Empty string clears a field.

        Setting ``number`` locks it (user-set) unless ``locked`` is passed.
        ``wire_type`` is the conductor type (MTW, THHN, …). Size is ``gauge``.
        """
        wire = self.find_wire(wire_id=wire_id, frm=frm, to=to, sheet_id=sheet_id)
        if number is not None:
            wire.number = str(number).strip()
            if locked is None:
                wire.number_locked = bool(wire.number)
        if locked is not None:
            wire.number_locked = bool(locked)
        if color is not None:
            wire.color = str(color).strip()
        if gauge is not None:
            wire.gauge = str(gauge).strip()
        if cable is not None:
            wire.cable = str(cable).strip()
        if wire_type is not None:
            wire.wire_type = str(wire_type).strip()
        if number is not None:
            self._auto_number_wires()
        return wire

    def save(self, path: Path | str | None = None) -> Path:
        # "" is not a new path. Save keeps writing the open file.
        if path is None or not str(path).strip():
            chosen: Path | str | None = self.path
        else:
            chosen = path
        dest = absolute_project_path(chosen)
        if dest is None:
            raise ValueError("no path given; pass path= or save after Session.open")
        self._refuse_unsafe_save(dest)
        previous = self.path
        previous_logo = self.project.title_block.logo
        media = self._relocated_paths()
        self.path = dest
        try:
            self._relocate_logo()
            from panelcad.reconstruct.service import relocate_files

            relocate_files(self, previous=previous if previous and previous != dest else None)
            props = getattr(self.project, "properties", None)
            if isinstance(props, dict):
                from panelcad.library.panel import LAYOUT_READY_SKIPPED_KEY

                props.pop(LAYOUT_READY_SKIPPED_KEY, None)
            store = getattr(self.project, "reconstruct", None)
            if store is not None:
                for photo in store.photos:
                    photo.source_path = ""
            try:
                from panelcad.needs_info import write_resolved_library

                write_resolved_library(self)
            except Exception as exc:  # noqa: BLE001 — a library sidecar must not block save
                _log_io("error", f"needs_info_library {_safe_io_error(exc)}")
            self._push_share_event(dest, previous)
            try:
                save_project(self.project, dest)
            except Exception:
                self._pop_share_event()
                raise
        except Exception:
            self.path = previous
            self.project.title_block.logo = previous_logo
            self._restore_relocated_paths(media)
            raise
        self.recovered = False
        try:
            self._disk_stamp = project_share.file_stamp(dest)
        except OSError as exc:
            # The bytes are already replaced. A read that still fails is the
            # file being busy, not a folder we could not write.
            raise project_share.FileBusy() from exc
        self._stamped_path = dest
        self.conflict_copies = project_share.find_conflict_copies(dest)
        self._claim_saved_path(previous, dest)
        _log_io("save", dest)
        _note_project(self)
        _remember_saved(self)
        return dest

    def _prepare_share(self) -> None:
        """Remember the file hash, warn about sync copies, and take a lock when shared."""

        path = self.path
        if path is None:
            return
        from panelcad.diag.paths import is_under_recovery

        if is_under_recovery(path):
            return
        from panelcad.io.winpath import project_presence

        if project_presence(path) == "file":
            self._stamped_path = path
            self._disk_stamp = project_share.file_stamp(path)
        self.conflict_copies = project_share.find_conflict_copies(path)
        claim = project_share.claim_project_lock(path, current_token=self._lock_token)
        self._apply_claim(claim, record=True)

    def _refuse_unsafe_save(self, dest: Path) -> None:
        from panelcad.diag.paths import is_under_recovery

        if is_under_recovery(dest):
            return
        if (
            self._stamped_path is not None
            and self._disk_stamp
            and self._same_file(dest, self._stamped_path)
            and project_share.file_stamp(dest, retry=True) != self._disk_stamp
        ):
            self.conflict_copies = project_share.find_conflict_copies(dest)
            raise project_share.ProjectChanged(dest, self.conflict_copies)
        if self._foreign_lock(dest) or (
            self.read_only and (self.path is None or self._same_file(dest, self.path))
        ):
            holder = self.lock_holder or project_share.read_project_lock(dest)
            raise project_share.ReadOnlyProject(holder)

    def _foreign_lock(self, dest: Path) -> bool:
        if not project_share.folder_is_shared(dest):
            return False
        info = project_share.read_project_lock(dest)
        if info is None or info.unreadable:
            return False
        if self._lock_token and info.token == self._lock_token:
            return False
        if project_share.lock_is_ours(info) or project_share.lock_is_stale(info):
            return False
        return True

    def _push_share_event(self, dest: Path, previous: Path | None) -> None:
        from panelcad.diag.paths import is_under_recovery

        if is_under_recovery(dest):
            self._share_restore = None
            return
        summary = (self._share_summary or "").strip()
        self._share_summary = ""
        if not summary:
            copied = previous is not None and not self._same_file(previous, dest)
            summary = "saved a copy" if copied else "saved"
        from panelcad.core.models import ShareEvent

        event = ShareEvent(
            user=project_share.current_user(),
            host=project_share.current_host(),
            time=project_share.now_iso(),
            summary=summary[:200],
        )
        self._share_restore = (list(self.project.activity), self.project.last_saved)
        self.project.activity.append(event)
        if len(self.project.activity) > project_share.ACTIVITY_KEEP:
            self.project.activity = self.project.activity[-project_share.ACTIVITY_KEEP :]
        self.project.last_saved = event

    def _pop_share_event(self) -> None:
        previous = self._share_restore
        self._share_restore = None
        if previous is None:
            return
        self.project.activity = previous[0]
        self.project.last_saved = previous[1]

    def _relocated_paths(self) -> tuple[list[tuple[str, str, str]], list[str]] | None:
        """Photo and print paths, so a failed Save As can put them back."""

        store = getattr(self.project, "reconstruct", None)
        if store is None:
            return None
        photos = [
            (photo.path, photo.source_path, photo.resolved_path) for photo in store.photos
        ]
        prints = [item.path for item in store.prints]
        return photos, prints

    def _restore_relocated_paths(
        self, saved: tuple[list[tuple[str, str, str]], list[str]] | None
    ) -> None:
        if saved is None:
            return
        store = getattr(self.project, "reconstruct", None)
        if store is None:
            return
        photos, prints = saved
        for photo, state in zip(store.photos, photos):
            photo.path, photo.source_path, photo.resolved_path = state
        for item, path in zip(store.prints, prints):
            item.path = path

    def _claim_saved_path(self, previous: Path | None, dest: Path) -> None:
        from panelcad.diag.paths import is_under_recovery

        if is_under_recovery(dest):
            return
        if previous is not None and not self._same_file(previous, dest) and self._lock_token:
            old = self._lock_token
            self._lock_token = ""
            project_share.release_project_lock(previous, old)
        if not project_share.folder_is_shared(dest):
            self.share_mode = "local"
            self.read_only = False
            self.lock_holder = None
            return
        claim = project_share.claim_project_lock(dest, current_token=self._lock_token)
        self._apply_claim(claim, record=False)

    def _apply_claim(self, claim: project_share.LockClaim, *, record: bool) -> None:
        self.share_mode = claim.mode
        if claim.mode == "local":
            self.read_only = False
            self.lock_holder = None
            return
        if claim.token:
            self._lock_token = claim.token
        holder = claim.holder
        if claim.mode in {"readonly", "unreadable"}:
            self.read_only = True
            self.lock_holder = holder
        else:
            self.read_only = False
            self.lock_holder = None
        if claim.mode == "stale" and holder is not None:
            self.share_notice = f"Replaced a stale lock from {holder.label()}."
            if record:
                who = holder.user or "someone"
                host = holder.host or "another PC"
                self._share_summary = f"replaced a stale lock from {who} on {host}"
        elif claim.mode == "takeover" and holder is not None:
            self.share_notice = (
                f"Took over the lock from {holder.label()}. "
                "Their unsaved work is not in this file."
            )
            if record:
                who = holder.user or "someone"
                host = holder.host or "another PC"
                self._share_summary = f"took over the lock from {who} on {host}"
        elif claim.mode == "readonly" and holder is not None and not holder.unreadable:
            self.share_notice = f"Read-only. Locked by {holder.label()}."
        elif claim.mode in {"readonly", "unreadable"}:
            self.share_notice = "The lock file could not be read. Opened read-only."

    def _same_file(self, left: Path | str | None, right: Path | str | None) -> bool:
        if left is None or right is None:
            return False
        try:
            return Path(left).resolve() == Path(right).resolve()
        except OSError:
            return Path(left) == Path(right)

    def release_share_lock(self) -> None:
        """Delete the lock when this session is the last holder. Safe to call twice."""

        token = self._lock_token
        self._lock_token = ""
        if token:
            project_share.release_project_lock(self.path, token)

    def take_over_lock(self) -> None:
        """Replace a live lock after the operator has been warned."""

        if self.path is None:
            raise ValueError("no project file")
        old = self._lock_token
        claim = project_share.claim_project_lock(self.path, take_over=True, current_token="")
        self._apply_claim(claim, record=True)
        if old and old != self._lock_token:
            project_share.release_project_lock(self.path, old)

    def heartbeat_share_lock(self) -> None:
        """Refresh our lock. A lock someone else took makes this session read-only."""

        token = self._lock_token
        if not token or self.path is None:
            return
        if project_share.heartbeat_project_lock(self.path, token):
            return
        info = project_share.read_project_lock(self.path)
        self._lock_token = ""
        project_share.release_project_lock(self.path, token)
        taken = (
            info is not None
            and not info.unreadable
            and not project_share.lock_is_ours(info)
            and not project_share.lock_is_stale(info)
        )
        if not taken:
            return
        self.read_only = True
        self.share_mode = "readonly"
        self.lock_holder = info
        self.share_notice = f"Lock taken by {info.label()}."

    def disk_matches(self) -> bool:
        """True when the open file's bytes still match the last open or save.

        One read, with no save-sized retry. ``OSError`` when the file is busy,
        so the disk watcher can skip that tick. A busy file is not reported as
        unchanged.
        """

        if self._stamped_path is None or not self._disk_stamp or self.path is None:
            return True
        if not self._same_file(self.path, self._stamped_path):
            return True
        return (
            project_share.file_stamp(self.path, pauses=project_share.WATCH_PAUSES)
            == self._disk_stamp
        )

    def preview_diff(self) -> str:
        """Text diff of the file on disk and this session. Does not write."""

        from panelcad.io.yaml_format import dump_project

        if self.path is None or not self.path.is_file():
            disk = ""
            name = "project"
        else:
            disk = self.path.read_text(encoding="utf-8")
            name = self.path.name
        return project_share.unified_diff_text(disk, dump_project(self.project), name)

    def report_bug(
        self,
        title: str,
        description: str,
        *,
        include_project: bool = False,
        include_screenshot: bool = False,
        send: bool = False,
        steps: str = "",
        contact: str = "",
        screenshot_png: bytes | None = None,
        traceback: str = "",
        crash_id: str = "",
        created: str = "",
        include_log: bool = True,
        include_system: bool = True,
        settings: object | None = None,
        transport: object | None = None,
    ) -> dict[str, Any]:
        """Write a bug-report zip. ``send`` posts it when a reporter is configured."""

        from panelcad.diag.report import ReportRequest, create_bug_report, recent_actions_text
        from panelcad.diag.settings import BugSettings

        png = screenshot_png
        if include_screenshot and not png:
            png = _grab_screenshot()
        project_bytes = None
        project_source = ""
        if include_project:
            project_bytes, project_source = _project_snapshot(self)
        request = ReportRequest(
            title=str(title or ""),
            description=str(description or ""),
            steps=str(steps or ""),
            contact=str(contact or ""),
            include_log=include_log,
            include_system=include_system,
            include_screenshot=bool(include_screenshot and png),
            include_project=bool(include_project and project_bytes),
            screenshot_png=png if include_screenshot else None,
            project_bytes=project_bytes if include_project else None,
            project_source=project_source,
            traceback=str(traceback or ""),
            created=str(created or ""),
            crash_id=str(crash_id or ""),
            recent_actions=recent_actions_text(self),
        )
        cfg = settings if isinstance(settings, BugSettings) else None
        result = create_bug_report(request, send=send, settings=cfg, transport=transport)
        _log_io("report_bug", result.get("path"))
        return result

    def app_settings(self) -> dict[str, int]:
        """Settings that apply to this install, not one drawing."""

        from panelcad.diag.settings import autosave_seconds

        return {"autosave_seconds": autosave_seconds()}

    def set_autosave_seconds(self, seconds: int) -> dict[str, int]:
        """Save how often a dirty drawing is copied into the recovery folder.

        ``0`` turns autosave off. ``PANELCAD_AUTOSAVE_SECONDS`` still wins over
        the saved value; the returned interval is the one in effect.
        """

        from panelcad.diag.settings import set_autosave_seconds

        set_autosave_seconds(seconds)
        return self.app_settings()

    def sheet_index(self, sheet_id: str | None = None) -> int:
        sheet = self.sheet(sheet_id)
        return self.project.sheets.index(sheet)

    @_traced_path("export_svg")
    def export_svg(self, path: Path | str, sheet_id: str | None = None) -> Path:
        sheet = self.sheet(sheet_id)
        svg = sheet_to_svg(
            sheet,
            self.catalog,
            project=self.project,
            sheet_index=self.sheet_index(sheet.id),
            sheet_count=len(self.project.sheets),
            base_dir=self.path.parent if self.path else Path.cwd(),
        )
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(svg, encoding="utf-8")
        return dest

    @_traced_path("export_pdf")
    def export_pdf(self, path: Path | str, sheet_id: str | None = None) -> Path:
        """Write drawing sheets only (one PDF page each). Use print_package for the full job."""
        from panelcad.render.pdf import project_to_pdf

        _ensure_qt_app()
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        sheets = [self.sheet(sheet_id)] if sheet_id else list(self.project.sheets)
        project_to_pdf(
            self.project,
            self.catalog,
            dest,
            sheets=sheets,
            base_dir=self.path.parent if self.path else Path.cwd(),
        )
        return dest

    @_traced_path("export_dxf")
    def export_dxf(self, path: Path | str, sheet_id: str | None = None) -> Path:
        """Write one AutoCAD DXF (R2010, mm). Default is the active sheet, like SVG."""
        from panelcad.render.dxf import sheet_to_dxf

        sheet = self.sheet(sheet_id)
        dest = Path(path)
        if dest.suffix.lower() != ".dxf":
            dest = dest / f"{sheet.id}.dxf" if dest.suffix == "" else dest.with_suffix(".dxf")
        return sheet_to_dxf(
            dest,
            sheet,
            self.catalog,
            project=self.project,
            sheet_index=self.sheet_index(sheet.id),
            sheet_count=len(self.project.sheets),
            base_dir=self.path.parent if self.path else Path.cwd(),
        )

    @_traced_path("export_dxf")
    def export_dxfs(self, dest: Path | str, sheet_id: str | None = None) -> list[Path]:
        """Write one DXF per sheet (or only ``sheet_id``). ``dest`` is a folder or a .dxf stem."""
        from panelcad.render.dxf import project_to_dxf

        sheets = [self.sheet(sheet_id)] if sheet_id else list(self.project.sheets)
        return project_to_dxf(
            self.project,
            self.catalog,
            dest,
            sheets=sheets,
            base_dir=self.path.parent if self.path else Path.cwd(),
        )

    def cutout_drawings(self, options: dict[str, Any] | None = None) -> list[Any]:
        """Sheets for the door, backplate, and walls. Does not write the project."""

        from panelcad.core.cutout import apply_cutout_options, build_drawings

        settings = apply_cutout_options(self.project, options)
        return build_drawings(self.project, self.catalog, settings)

    def cutouts_generate(self, options: dict[str, Any] | None = None, *, persist: bool = False) -> dict[str, Any]:
        """Build cutout sheets. ``persist`` stores datum, view, units, and clearances (one undo)."""

        from panelcad.core.cutout import drawing_to_dict, write_cutout_options

        payload = dict(options or {})
        if persist:
            with self.command("cutout settings"):
                write_cutout_options(self.project, payload)
            try:
                self.emit("cutouts_generate", "cutout drawing settings")
            except Exception:  # noqa: BLE001
                pass
            view_options = {"surfaces": payload["surfaces"]} if "surfaces" in payload else None
        else:
            view_options = payload or None
        drawings = self.cutout_drawings(view_options)
        return {
            "count": len(drawings),
            "surfaces": [drawing.surface for drawing in drawings],
            "drawings": [drawing_to_dict(drawing) for drawing in drawings],
        }

    @_traced_path("cutouts_export_dxf")
    def cutouts_export_dxf(self, path: Path | str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write one 1:1 DXF per surface. Layers CUT, DRILL, DIM, TEXT, OUTLINE."""

        from panelcad.render.cutout_dxf import write_cutout_dxfs

        drawings = self.cutout_drawings(options)
        exportable = [drawing for drawing in drawings if drawing.features]
        skipped = [drawing.surface for drawing in drawings if not drawing.features]
        if not exportable:
            return {
                "paths": [],
                "surfaces": [],
                "skipped": skipped,
                "units": (drawings[0].units if drawings else "mm"),
            }
        paths = write_cutout_dxfs(path, exportable, self.project)
        return {
            "paths": [str(item) for item in paths],
            "surfaces": [drawing.surface for drawing in exportable],
            "skipped": skipped,
            "units": exportable[0].units,
        }

    @_traced_path("cutouts_export_pdf")
    def cutouts_export_pdf(self, path: Path | str, options: dict[str, Any] | None = None) -> Path:
        """Write the cutout sheets to one PDF (1:1 when the sheet fits)."""

        from panelcad.render.cutout_draw import write_cutout_pdf

        _ensure_qt_app()
        drawings = self.cutout_drawings(options)
        return write_cutout_pdf(path, drawings, self.project)

    def part_set_cutout(
        self,
        ref: str,
        cutout: dict[str, Any] | None = None,
        *,
        sheet_id: str | None = None,
        unknown: bool | None = None,
        clear: bool = False,
        surface: str | None = None,
        include_key: bool | None = None,
        min_center_x_mm: float | None = None,
        min_center_y_mm: float | None = None,
        centers_interchangeable: bool | None = None,
    ) -> dict[str, Any]:
        """Store a cutout on ``ref``. A catalog assignment replaces this override.

        ``clear`` drops the override so the library record applies again.
        ``unknown=True`` marks the opening so nothing is invented, and keeps the
        previous geometry, key, and centre minimums for ``unknown=False``.
        ``cutout`` is a mapping of published or shop-entered geometry
        (rectangular, round, or D, plus drills). One undo via ``_ACTIVITY_METHODS``.
        """

        from panelcad.core.cutout import SURFACES, parse_cutout

        spacing = min_center_x_mm is not None or min_center_y_mm is not None or centers_interchangeable is not None
        if not clear and unknown is None and cutout is None and surface is None and include_key is None and not spacing:
            raise ValueError(
                "part_set_cutout needs cutout, unknown, clear, surface, include_key, or a centre minimum"
            )
        stored: dict[str, Any] | None = None
        restore = unknown is False and cutout is None and not clear
        if unknown:
            stored = {"unknown": True, "note": "Cutout marked unknown. Edit before manufacturing."}
        elif cutout is not None:
            stored = parse_cutout(cutout).to_dict()
        if surface is not None:
            token = str(surface).strip().lower()
            if token not in SURFACES:
                raise ValueError(f"surface must be one of {', '.join(SURFACES)}")
            surface = token
        sheets = [self.sheet(sheet_id)] if sheet_id else list(self.project.sheets)
        updated: list[str] = []
        found = False
        for sheet in sheets:
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            found = True
            if clear:
                comp.properties.pop("cutout", None)
                comp.properties.pop("cutout_restore", None)
                comp.properties.pop("include_key", None)
                comp.properties.pop("min_center_x_mm", None)
                comp.properties.pop("min_center_y_mm", None)
                comp.properties.pop("centers_interchangeable", None)
            elif restore:
                _restore_cutout_props(comp)
            elif unknown:
                _stash_cutout_props(comp)
                comp.properties["cutout"] = stored
            elif stored is not None:
                comp.properties.pop("cutout_restore", None)
                comp.properties["cutout"] = stored
            if surface is not None:
                comp.properties["surface"] = surface
            if include_key is not None:
                comp.properties["include_key"] = bool(include_key)
            if min_center_x_mm is not None:
                comp.properties["min_center_x_mm"] = float(min_center_x_mm)
            if min_center_y_mm is not None:
                comp.properties["min_center_y_mm"] = float(min_center_y_mm)
            if centers_interchangeable is not None:
                comp.properties["centers_interchangeable"] = bool(centers_interchangeable)
            updated.append(sheet.id)
        if not found:
            raise KeyError(f"no component '{ref}'")
        return {
            "id": ref,
            "sheets": updated,
            "cleared": bool(clear),
            "unknown": bool(unknown),
            "cutout": None if clear else stored,
            "surface": surface,
            "include_key": None if include_key is None else bool(include_key),
            "min_center_x_mm": None if min_center_x_mm is None else float(min_center_x_mm),
            "min_center_y_mm": None if min_center_y_mm is None else float(min_center_y_mm),
            "centers_interchangeable": None
            if centers_interchangeable is None
            else bool(centers_interchangeable),
        }

    def _ensure_rev(self) -> Revision:
        if not self.project.title_block.revisions:
            self.project.title_block.revisions.append(Revision(rev="A"))
        return self.project.title_block.revisions[0]

    def set_control_voltage(self, value: Any) -> float | None:
        """Store one control-circuit voltage on the project and every ladder sheet.

        ``""`` clears it. Text that is not a voltage is rejected and the stored
        value stays. This is a stated voltage, not a library default.
        """

        import math

        from panelcad.core.checks import parse_volts

        def _reject() -> None:
            self.forget_noop_checkpoint()
            raise ValueError(f"Control voltage must be a number of volts, not {value!r}")

        number: float | None
        if value in (None, ""):
            number = None
        else:
            parsed = parse_volts(value)
            if parsed and parsed[0] > 0:
                number = float(parsed[0])
            else:
                try:
                    number = float(str(value).strip())
                except (TypeError, ValueError):
                    _reject()
            if not math.isfinite(number) or number <= 0:
                _reject()
        self.project.control_voltage = number
        for sheet in self.project.sheets:
            if sheet.kind in {"schematic", "io"}:
                sheet.control_voltage = number
        return number

    def set_title_block(self, **fields: Any) -> dict[str, Any]:
        """Fill title-block boxes. Unknown keys raise KeyError."""
        tb = self.project.title_block
        aliases = {
            "revision": "rev",
            "rev_date": "date",
            "rev_description": "rev_description",
            "dwg": "drawing_number",
            "drawing_no": "drawing_number",
            "drawn": "drawn_by",
            "checked": "checked_by",
            "approved": "approved_by",
        }
        for raw_key, value in fields.items():
            key = aliases.get(raw_key, raw_key)
            if key == "name":
                self.project.name = str(value)
            elif key == "title":
                self.sheet().title = str(value)
            elif key == "description":
                self.sheet().description = str(value)
            elif key == "rev":
                self._ensure_rev().rev = str(value)
            elif key == "date":
                self._ensure_rev().date = str(value)
            elif key == "rev_description":
                self._ensure_rev().description = str(value)
            elif key == "logo":
                if value:
                    p = Path(str(value))
                    if p.is_file():
                        self.set_logo(p)
                    else:
                        tb.logo = str(value).replace("\\", "/")
                else:
                    tb.logo = ""
            elif key == "standard":
                from panelcad.library.catalog import normalize_standard

                tb.standard = normalize_standard(str(value))
            elif key == "drawing_style":
                from panelcad.core.models import normalize_drawing_style

                tb.drawing_style = normalize_drawing_style(str(value))  # type: ignore[assignment]
            elif key == "coil_style":
                token = str(value).strip().lower()
                tb.coil_style = "square" if token == "square" else "circle"
            elif key in {"rails", "ladder_rails"}:
                from panelcad.core.ladder import parse_rail_pair, rail_snapshot, rename_rail_endpoints, sync_sheet_rails

                pair = parse_rail_pair(str(value))
                if pair:
                    before = rail_snapshot(self.project)
                    tb.rail_hot, tb.rail_return = pair
                    sync_sheet_rails(self.project)
                    rename_rail_endpoints(self.project, before)
            elif key in {"rail_hot", "hot", "left_rail"}:
                from panelcad.core.ladder import rail_snapshot, rename_rail_endpoints, sync_sheet_rails

                before = rail_snapshot(self.project)
                tb.rail_hot = str(value).strip() or "L1"
                sync_sheet_rails(self.project)
                rename_rail_endpoints(self.project, before)
            elif key in {"rail_return", "neutral", "right_rail", "return"}:
                from panelcad.core.ladder import rail_snapshot, rename_rail_endpoints, sync_sheet_rails

                before = rail_snapshot(self.project)
                tb.rail_return = str(value).strip() or "L2"
                sync_sheet_rails(self.project)
                rename_rail_endpoints(self.project, before)
            elif key == "ref_prefixes":
                if value is None or value == "":
                    self.project.ref_prefixes = {}
                elif isinstance(value, dict):
                    self.project.ref_prefixes = {str(k): str(v).strip() for k, v in value.items() if str(v).strip()}
                else:
                    raise ValueError("ref_prefixes must be a map of category → prefix")
            elif hasattr(tb, key):
                setattr(tb, key, str(value))
                if key == "drawn_by":
                    self._ensure_rev().drawn = str(value)
                elif key == "checked_by":
                    self._ensure_rev().checked = str(value)
                elif key == "approved_by":
                    self._ensure_rev().approved = str(value)
            else:
                raise KeyError(f"unknown title-block field '{raw_key}'")
        from panelcad.core.ladder import stamp_drawing_style

        stamp_drawing_style(self.project)
        if tb.drawing_style == "ladder":
            from panelcad.core.ladder import sync_sheet_rails

            sync_sheet_rails(self.project)
        return tb.model_dump()

    def issue_revision(
        self,
        description: str = "",
        rev: str | None = None,
        scheme: str | None = None,
        date: str | None = None,
        drawn: str = "",
        checked: str = "",
        approved: str = "",
        clear_clouds: bool | None = None,
        auto_clouds: bool = True,
    ) -> dict[str, Any]:
        """Snapshot the project, bump the rev, and fill the title-block revision table."""

        from panelcad.core.revisions import issue_revision as _issue
        from panelcad.core.revisions import normalize_scheme, resolve_revision_token

        if scheme:
            normalize_scheme(scheme)
        token = resolve_revision_token(self.project, rev=rev, scheme=scheme)

        def run() -> dict[str, Any]:
            return _issue(
                self.project,
                self.catalog,
                description=description,
                rev=rev,
                scheme=scheme,
                date=date,
                drawn=drawn,
                checked=checked,
                approved=approved,
                clear_clouds=clear_clouds,
                auto_clouds=auto_clouds,
            )

        return self._undoable("issue_revision", f"rev {token}", run)

    def list_revisions(self) -> dict[str, Any]:
        from panelcad.core.revisions import list_revisions as _list

        return _list(self.project)

    def compare_revisions(
        self,
        left: str,
        right: str,
        csv_path: Path | str | None = None,
        pdf_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Structured diff of two issued revisions, or ``working`` for the open drawing."""

        from panelcad.core.revisions import compare_revisions as _compare

        report = _compare(self.project, self.catalog, left, right)
        if csv_path:
            from panelcad.render.revision_report import write_compare_csv

            report["csv"] = str(write_compare_csv(csv_path, report))
        if pdf_path:
            from panelcad.render.revision_report import write_compare_pdf

            _ensure_qt_app()
            report["pdf"] = str(write_compare_pdf(pdf_path, report, self.project))
        return report

    def add_revision_cloud(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        sheet_id: str | None = None,
        rev: str = "",
    ) -> dict[str, Any]:
        from panelcad.core.revisions import _normalize_rect, add_revision_cloud as _add

        _normalize_rect(float(x), float(y), float(w), float(h))
        sheet = self.sheet(sheet_id)

        def run() -> dict[str, Any]:
            return _add(self.project, sheet.id, x, y, w, h, rev=rev)

        return self._undoable("add_revision_cloud", "revision cloud", run)

    def clear_revision_clouds(self, rev: str | None = None) -> dict[str, Any]:
        from panelcad.core.revisions import clear_revision_clouds as _clear

        def run() -> dict[str, Any]:
            return _clear(self.project, rev=rev)

        return self._undoable("clear_revision_clouds", "clear revision clouds", run)

    def set_logo(self, source: Path | str) -> Path:
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(f"logo file not found: {src}")
        self.project.title_block.logo = str(src.resolve())
        self._relocate_logo()
        return Path(self.project.title_block.logo)

    def clear_logo(self) -> None:
        self.project.title_block.logo = ""

    def _relocate_logo(self) -> None:
        """Copy the logo next to the project so drawings stay portable."""
        if self.path is None:
            return
        logo = self.project.title_block.logo
        if not logo:
            return
        src = Path(logo)
        if not src.is_file():
            alt = self.path.parent / logo
            if alt.is_file():
                src = alt
            else:
                return
        dest_dir = self.path.parent / "assets"
        dest = dest_dir / f"company_logo{src.suffix.lower() or '.png'}"
        if src.resolve() != dest.resolve():
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        self.project.title_block.logo = (Path("assets") / dest.name).as_posix()

    def request_oem_update(self, refs: list[str] | None = None) -> dict[str, Any]:
        """Queue an OEM catalog pull for the AI (dims, pins, picture). refs=None means every part."""
        wanted = {str(r) for r in (refs or [])}
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sheet in self.project.sheets:
            for c in sheet.components:
                if c.id in seen:
                    continue
                if wanted and c.id not in wanted:
                    continue
                seen.add(c.id)
                device = self.catalog.get(c.library_id)
                c.properties["oem_status"] = "requested"
                targets.append(
                    {
                        "ref": c.id,
                        "sheet": sheet.id,
                        "library_id": c.library_id,
                        "name": device.name,
                        "manufacturer": str(c.properties.get("manufacturer") or device.properties.get("manufacturer") or ""),
                        "part_number": str(c.properties.get("part_number") or device.properties.get("part_number") or ""),
                        "catalog": str(c.properties.get("catalog") or device.properties.get("catalog") or ""),
                    }
                )
        if not targets:
            raise ValueError("no parts selected for OEM update")
        lines = [
            "OEM update requested. For each part pull manufacturer catalog data:",
            "width_mm, height_mm, depth_mm, connection-point x/y/side, and a product photo if available.",
            "Then apply with op update_oem (ref, width_mm, height_mm, depth_mm, pins?, picture?, manufacturer?, part_number?).",
            "The result is unconfirmed measured. A person must confirm_envelope, or assign_part a verified SKU, before the part is layout-ready.",
            "",
        ]
        for t in targets:
            pn = t["part_number"] or t["catalog"] or t["library_id"]
            lines.append(f"- {t['ref']}  {t['library_id']}  {t['name']}  PN {pn or 'unknown'}  sheet {t['sheet']}")
        text = "\n".join(lines)
        self.submit_prompt(text)
        self.emit("say", f"OEM update queued for {len(targets)} part(s)", extra={"oem": targets})
        return {"count": len(targets), "parts": targets}

    def apply_oem(
        self,
        ref: str,
        *,
        width_mm: float | None = None,
        height_mm: float | None = None,
        depth_mm: float | None = None,
        manufacturer: str | None = None,
        part_number: str | None = None,
        catalog: str | None = None,
        pins: list[dict[str, Any]] | None = None,
        picture: str | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply OEM catalog data the AI pulled (dims, pins, photo)."""
        if width_mm is None or height_mm is None or depth_mm is None:
            raise LayoutNotReadyError(
                f"{ref}: apply_oem requires width_mm, height_mm, and depth_mm. "
                "A verified SKU needs assign_part; a measured size needs confirm_envelope."
            )
        import math

        try:
            width = float(width_mm)
            height = float(height_mm)
            depth = float(depth_mm)
        except (TypeError, ValueError) as exc:
            raise LayoutNotReadyError(
                f"{ref}: apply_oem needs numeric width_mm, height_mm, and depth_mm"
            ) from exc
        if not (math.isfinite(width) and math.isfinite(height) and math.isfinite(depth)):
            raise LayoutNotReadyError(
                f"{ref}: apply_oem needs finite width_mm, height_mm, and depth_mm"
            )
        if min(width, height, depth) < 1.0:
            raise LayoutNotReadyError(
                f"{ref}: apply_oem needs width_mm, height_mm, and depth_mm of at least 1 mm"
            )
        if sheet_id is None:
            for sh in self.project.sheets:
                try:
                    sh.component(ref)
                    sheet_id = sh.id
                    break
                except KeyError:
                    continue
        props: dict[str, Any] = {"oem_status": "catalog"}
        if manufacturer:
            props["manufacturer"] = manufacturer
        if part_number:
            props["part_number"] = part_number
        if catalog:
            props["catalog"] = catalog
        self.set_component_properties(ref, props, sheet_id=sheet_id)
        panel = self.set_panel(
            ref,
            width_mm=width,
            height_mm=height,
            depth_mm=depth,
            dim_source="measured",
            sheet_id=sheet_id,
        )
        applied_pins = 0
        if pins:
            self.set_pins_locked(ref, False, sheet_id=sheet_id)
            for p in pins:
                pid = str(p.get("id") or p.get("pin") or "")
                if not pid or "x" not in p or "y" not in p:
                    continue
                try:
                    self.set_pin(ref, pid, float(p["x"]), float(p["y"]), side=p.get("side"), sheet_id=sheet_id)
                    applied_pins += 1
                except (KeyError, ValueError):
                    continue
            self.set_pins_locked(ref, True, sheet_id=sheet_id)
        pic = None
        if picture:
            pic = str(self.set_picture(ref, picture, sheet_id=sheet_id))
        return {"id": ref, "panel": panel.get("panel"), "pins": applied_pins, "picture": pic}

    def set_picture(self, ref: str, source: Path | str, sheet_id: str | None = None) -> Path:
        """Attach a photo shown only on panel_layout / door_layout footprints."""
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(f"picture file not found: {src}")
        comp = self.sheet(sheet_id).component(ref)
        if self.path is None:
            comp.picture = str(src.resolve())
            return src
        dest_dir = self.path.parent / "assets"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{comp.id}_pic{src.suffix.lower() or '.png'}"
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        comp.picture = (Path("assets") / dest.name).as_posix()
        return dest

    def clear_picture(self, ref: str, sheet_id: str | None = None) -> Component:
        comp = self.sheet(sheet_id).component(ref)
        comp.picture = ""
        return comp

    def apply_cut_sheet_pictures(self, folder: Path | str | None = None) -> dict[str, Any]:
        """Stamp OEM cut-sheet photos onto panel/door footprints. Print scans are skipped."""
        import re

        dest = Path(folder) if folder else (self.path.parent / "assets" if self.path else None)
        applied: list[str] = []
        skipped: list[str] = []
        if dest is None or not dest.is_dir():
            return {"applied": 0, "skipped": 0, "refs": applied, "folder": str(dest or "")}

        scan_bits = (
            "previous-print",
            "previous_print",
            "as-built",
            "asbuilt",
            "hdoor",
            "layout.photo",
            "scan",
        )
        datestamp = re.compile(r"20\d{6}[_-]\d")
        pic_slot = re.compile(r"(^|[^a-z])pic\d+($|[^a-z])", re.I)

        def _norm(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", text.lower())

        def _is_scan(name: str) -> bool:
            n = name.lower()
            if datestamp.search(n) or pic_slot.search(n):
                return True
            return any(bit in n for bit in scan_bits)

        files: list[Path] = []
        for path in sorted(dest.iterdir()):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            if _is_scan(path.stem):
                skipped.append(path.name)
                continue
            files.append(path)
        if not files:
            return {"applied": 0, "skipped": len(skipped), "refs": applied, "folder": str(dest)}

        by_key: dict[str, Path] = {}
        for path in files:
            by_key[_norm(path.stem)] = path

        for sheet in self.project.sheets:
            if sheet.kind not in ("panel_layout", "door_layout"):
                continue
            for comp in sheet.components:
                if comp.picture:
                    continue
                device = self.catalog.get(comp.library_id)
                keys = [
                    str(comp.properties.get("part_number") or ""),
                    str(comp.properties.get("catalog") or ""),
                    str(device.properties.get("part_number") or ""),
                    str(device.properties.get("catalog") or ""),
                    device.id,
                    device.library_id.split(".")[-1],
                ]
                hit: Path | None = None
                for key in keys:
                    if not key:
                        continue
                    hit = by_key.get(_norm(key))
                    if hit is not None:
                        break
                if hit is None:
                    continue
                try:
                    self.set_picture(comp.id, hit, sheet_id=sheet.id)
                    applied.append(comp.id)
                except (FileNotFoundError, KeyError):
                    continue
        return {
            "applied": len(applied),
            "skipped": len(skipped),
            "refs": applied,
            "folder": str(dest),
        }

    def _auto_number_wires(self) -> dict[str, Any] | None:
        """Fill wire numbers after a draw / connect / delete. Never runs on open or undo."""

        if self._restoring:
            return None
        from panelcad.core.wire_numbering import assign_wire_numbers

        return assign_wire_numbers(self.project, self.catalog, mode="incremental")

    def wire_numbering(self) -> dict[str, Any]:
        from panelcad.core.wire_numbering import settings_payload

        return settings_payload(self.project)

    def set_wire_numbering(
        self,
        *,
        scheme: str | None = None,
        enabled: bool | None = None,
        start: int | None = None,
        split_feedthrough: bool | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.wire_numbering import (
            release_feedthrough_numbers,
            resolve_settings,
            set_settings,
            settings_payload,
        )

        previous = resolve_settings(self.project).split_feedthrough
        set_settings(
            self.project,
            scheme=scheme,
            enabled=enabled,
            start=start,
            split_feedthrough=split_feedthrough,
        )
        if split_feedthrough is not None and bool(split_feedthrough) != bool(previous):
            release_feedthrough_numbers(self.project, self.catalog)
            self._auto_number_wires()
        return settings_payload(self.project)

    def number_wires(
        self,
        *,
        start: int = 1,
        overwrite: bool = False,
        scheme: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.electrical import number_wires

        return number_wires(
            self.project,
            start=start,
            overwrite=overwrite,
            scheme=scheme,
            catalog=self.catalog,
        )

    def wire_list_rows(self) -> list[dict[str, str]]:
        from panelcad.core.wire_numbering import wire_list_rows

        return wire_list_rows(self.project)

    def retag(self, mode: str = "duplicates", apply_prefixes: bool = False) -> list[dict[str, str]]:
        from panelcad.core.electrical import retag

        token = (mode or "duplicates").strip().lower()
        apply = bool(apply_prefixes) or token in {"standard", "prefixes", "nema"}
        label = "standard prefixes" if apply else token

        def _run() -> list[dict[str, str]]:
            return retag(self.project, self.catalog, mode=mode, apply_prefixes=apply)

        return self._undoable("retag", f"retag {label}", _run)

    def convert_standard_plan(
        self,
        target: str,
        sheets: list[str] | None = None,
        out: Path | str | None = None,
        workspace: Path | str | None = None,
        resume: Path | str | None = None,
        keep_numbers: bool = False,
    ) -> dict[str, Any]:
        """Plan an IEC <-> NEMA conversion. Writes a workspace JSON and does not edit the drawing."""

        from panelcad.convert.engine import conversion_plan

        return conversion_plan(
            self,
            target,
            sheets=sheets,
            out=out,
            workspace=workspace,
            resume=resume,
            keep_numbers=keep_numbers,
        )

    def convert_standard_next(self, workspace: Path | str | None = None) -> dict[str, Any]:
        """Convert the next unfinished chunk. One undo. Does not overwrite the source file."""

        from panelcad.convert.engine import next_chunk

        return self._undoable("convert_standard", "convert next chunk", lambda: next_chunk(self, workspace))

    def convert_standard_apply(
        self,
        workspace: Path | str | None = None,
        out: Path | str | None = None,
    ) -> dict[str, Any]:
        """Finish every pending chunk and save-as. One undo."""

        from panelcad.convert.engine import apply_conversion

        return self._undoable(
            "convert_standard",
            "apply drawing-standard conversion",
            lambda: apply_conversion(self, workspace, out=out),
        )

    def convert_standard(
        self,
        target: str,
        sheets: list[str] | None = None,
        out: Path | str | None = None,
        workspace: Path | str | None = None,
        keep_numbers: bool = False,
    ) -> dict[str, Any]:
        """Convert the project or selected sheets. One undo. Save-as; the source file stays put."""

        from panelcad.convert.engine import convert_standard

        return self._undoable(
            "convert_standard",
            f"convert drawing standard to {target}",
            lambda: convert_standard(
                self,
                target,
                sheets=sheets,
                out=out,
                workspace=workspace,
                keep_numbers=keep_numbers,
            ),
        )

    def convert_standard_review(self, workspace: Path | str | None = None) -> dict[str, Any]:
        from panelcad.convert.engine import review

        return review(self, workspace)

    def convert_standard_accept(self, chunk_id: str, workspace: Path | str | None = None) -> dict[str, Any]:
        from panelcad.convert.engine import accept_chunk

        return accept_chunk(self, chunk_id, workspace)

    def convert_standard_rerun(self, chunk_id: str, workspace: Path | str | None = None) -> dict[str, Any]:
        from panelcad.convert.engine import rerun_chunk

        return self._undoable(
            "convert_standard",
            f"re-run {chunk_id}",
            lambda: rerun_chunk(self, chunk_id, workspace),
        )

    def convert_standard_revert(self, chunk_id: str, workspace: Path | str | None = None) -> dict[str, Any]:
        from panelcad.convert.engine import revert_chunk

        return self._undoable(
            "convert_standard",
            f"revert {chunk_id}",
            lambda: revert_chunk(self, chunk_id, workspace),
        )

    def update_cross_references(self) -> list[dict[str, str]]:
        from panelcad.core.xref import update_cross_references

        return self._undoable(
            "cross_reference",
            "write cross-references",
            lambda: update_cross_references(self.project, self.catalog),
        )

    def cross_reference_rows(self) -> list[dict[str, str]]:
        from panelcad.core.xref import cross_reference_rows

        return cross_reference_rows(self.project, self.catalog)

    def cross_reference_report(self) -> list[dict[str, str]]:
        from panelcad.core.xref import cross_reference_report

        return cross_reference_report(self.project, self.catalog)

    @_traced_path("export_cross_reference")
    def export_cross_reference(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, self.cross_reference_report() or self.cross_reference_rows())
        return dest

    def cross_reference_settings(self) -> dict[str, Any]:
        from panelcad.core.xref import settings_payload

        return settings_payload(self.project)

    def set_cross_reference(
        self,
        *,
        nc_mark: str | None = None,
        enabled: bool | None = None,
        rung_pitch_mm: float | None = None,
        rung_numbers: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.xref import set_settings, settings_payload

        set_settings(
            self.project,
            nc_mark=nc_mark,
            enabled=enabled,
            rung_pitch_mm=rung_pitch_mm,
            rung_numbers=rung_numbers,
        )
        return settings_payload(self.project)

    def device_index(self, ref: str | None = None) -> dict[str, Any]:
        """Where every element of device *ref* lives (page, zone, terminals, wires)."""

        from panelcad.core.xref_index import device_index

        return device_index(self.project, self.catalog, ref)

    def device_index_rows(self, ref: str | None = None) -> list[dict[str, str]]:
        from panelcad.core.xref_index import device_index_rows

        return device_index_rows(self.project, self.catalog, ref)

    def lookup_zone(self, zone: str, *, sheet: str | None = None) -> dict[str, Any]:
        """What device/element is at printed page-zone *zone* (5-A2, A2, /1.3, 105)."""

        from panelcad.core.xref_index import lookup_zone

        return lookup_zone(self.project, self.catalog, zone, sheet=sheet)

    def trace_wire(self, number: str) -> dict[str, Any]:
        """Follow wire *number* across sheets: from/to, pages, devices."""

        from panelcad.core.xref_index import trace_wire

        return trace_wire(self.project, self.catalog, number)

    def circuit_for(self, ref: str) -> dict[str, Any]:
        """Control and power path for *ref*, from the drawing only."""

        from panelcad.core.troubleshoot import circuit_for

        return circuit_for(self.project, self.catalog, ref)

    def checkpoints(self, ref: str, symptom: str) -> dict[str, Any]:
        """Ordered LIVE and DE-ENERGIZED checks for *symptom* on *ref*."""

        from panelcad.core.troubleshoot import checkpoints

        return checkpoints(self.project, self.catalog, ref, symptom)

    def upstream(self, endpoint: str) -> dict[str, Any]:
        """Devices and wires from *endpoint* (``ref.pin``) toward the supply."""

        from panelcad.core.troubleshoot import upstream

        return upstream(self.project, self.catalog, endpoint)

    def downstream(self, endpoint: str) -> dict[str, Any]:
        """Devices and wires from *endpoint* (``ref.pin``) toward the load or return."""

        from panelcad.core.troubleshoot import downstream

        return downstream(self.project, self.catalog, endpoint)

    def plc_point(self, address: str) -> dict[str, Any]:
        """One PLC I/O point by address, point label, or tag on the drawing."""

        from panelcad.core.troubleshoot import plc_point

        return plc_point(self.project, self.catalog, address)

    def likely_causes(
        self,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ranked causes. A first wrong reading narrows the fault to that segment."""

        from panelcad.core.troubleshoot import likely_causes

        return likely_causes(self.project, self.catalog, ref, symptom, readings)

    def export_troubleshoot(self, path: Path | str) -> Path:
        """Write the open ``troubleshoot.json`` document. Does not change the project file."""

        from panelcad.core.troubleshoot import write_troubleshoot

        return write_troubleshoot(self.project, self.catalog, path)

    def append_troubleshoot_log(
        self,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
        when: str = "",
        project_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Append a readings entry beside the project. The project file is not written."""

        from panelcad.core.troubleshoot import append_troubleshoot_log
        from panelcad.core.xref_index import QueryError

        src = project_path if project_path is not None else self.path
        if src is None:
            raise QueryError("save the project before writing the troubleshooting log")
        return append_troubleshoot_log(
            self.project,
            self.catalog,
            src,
            ref=ref,
            symptom=symptom,
            readings=readings,
            who=who,
            note=note,
            when=when,
        )

    def troubleshoot_guide(
        self,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Guided next check, fault window, and causes. Same payload as the dock."""

        from panelcad.core.troubleshoot_guide import guide

        return guide(self.project, self.catalog, ref, symptom, readings)

    def export_troubleshoot_csv(
        self,
        path: Path | str,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
    ) -> Path:
        """CSV of the guided session. Does not modify the project file."""

        from panelcad.core.troubleshoot_guide import session_rows, write_session_csv

        payload = self.troubleshoot_guide(ref, symptom, readings)
        return write_session_csv(path, session_rows(payload, who=who, note=note))

    def export_troubleshoot_pdf(
        self,
        path: Path | str,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
    ) -> Path:
        """PDF of the guided session, opening with the lockout text."""

        from panelcad.render.troubleshoot_log import write_session_pdf

        payload = self.troubleshoot_guide(ref, symptom, readings)
        return write_session_pdf(
            path,
            payload,
            who=who,
            note=note,
            project_name=self.project.name,
        )

    def phase_colors(self) -> dict[str, Any]:
        from panelcad.core.phase_color import phase_color_payload

        return phase_color_payload(self.project)

    def set_phase_colors(self, **fields: Any) -> dict[str, Any]:
        """Project phase colors for 3-phase power sheets. Empty system_voltage follows the drawing."""

        from panelcad.core.phase_color import update_phase_colors

        return update_phase_colors(self.project, **fields)

    def terminal_strip_rows(self) -> list[dict[str, str]]:
        from panelcad.core.electrical import terminal_strip_rows

        return terminal_strip_rows(self.project, self.catalog)

    def sequence_terminal_strip(self, strip: str, *, rename: bool = False) -> list[dict[str, str]]:
        from panelcad.core.electrical import sequence_terminal_strip
        from panelcad.core.terminal_strip import sync_strip_panel

        rows = sequence_terminal_strip(self.project, self.catalog, strip, rename=rename)
        sync_strip_panel(self, strip)
        return rows

    def jumper_terminals(self, refs: list[str], group: str | None = None) -> dict[str, Any]:
        from panelcad.core.electrical import jumper_terminals

        return self._undoable(
            "jumper_terminals",
            f"jumper {', '.join(refs)}",
            lambda: jumper_terminals(self.project, refs, group=group, catalog=self.catalog),
        )

    def clear_terminal_jumpers(
        self,
        refs: list[str] | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.jumper import clear_jumpers

        return self._undoable(
            "clear_terminal_jumpers",
            "clear terminal jumpers",
            lambda: clear_jumpers(self.project, self.catalog, refs=refs, group=group),
        )

    def jumper_rows(self, strip: str | None = None) -> list[dict[str, str]]:
        from panelcad.core.jumper import jumper_bom_rows

        return jumper_bom_rows(self.project, self.catalog, strip=strip)

    def list_terminal_strips(self) -> list[dict[str, Any]]:
        from panelcad.core.terminal_strip import list_terminal_strips

        return list_terminal_strips(self.project, self.catalog)

    def open_terminal_strip(self, strip: str) -> dict[str, Any]:
        from panelcad.core.terminal_strip import open_terminal_strip

        return self._undoable(
            "open_terminal_strip",
            f"open terminal strip {strip}",
            lambda: open_terminal_strip(self, strip),
        )

    def add_terminal(
        self,
        strip: str,
        library_id: str = "iec.terminals.x_2p5",
        after: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.terminal_strip import add_terminal

        return self._undoable(
            "add_terminal",
            f"add terminal on {strip}",
            lambda: add_terminal(self, strip, library_id=library_id, after=after),
        )

    def reorder_terminals(self, strip: str, refs: list[str]) -> dict[str, Any]:
        from panelcad.core.terminal_strip import reorder_terminals

        return self._undoable(
            "reorder_terminals",
            f"reorder terminal strip {strip}",
            lambda: reorder_terminals(self, strip, refs),
        )

    def move_terminal(self, strip: str, ref: str, direction: str = "later") -> dict[str, Any]:
        from panelcad.core.terminal_strip import move_terminal

        return self._undoable(
            "move_terminal",
            f"move {ref} {direction} on {strip}",
            lambda: move_terminal(self, strip, ref, direction),
        )

    def retag_terminal_strip(self, strip: str) -> dict[str, Any]:
        from panelcad.core.terminal_strip import retag_terminal_strip

        return self._undoable(
            "retag_terminal_strip",
            f"retag terminal strip {strip}",
            lambda: retag_terminal_strip(self, strip),
        )

    def add_strip_accessory(self, strip: str, kind: str, after: str | None = None) -> dict[str, Any]:
        from panelcad.core.terminal_strip import add_strip_accessory

        return self._undoable(
            "add_strip_accessory",
            f"add {kind} on {strip}",
            lambda: add_strip_accessory(self, strip, kind, after=after),
        )

    def delete_strip_component(self, ref: str) -> dict[str, Any]:
        """Remove a strip block from the schematic and the panel. One undo."""

        from panelcad.core.terminal_strip import remove_strip_component

        return self._undoable(
            "delete_strip_component",
            f"delete {ref} from the terminal strip",
            lambda: remove_strip_component(self, ref),
        )

    @_traced_path("export_terminal_strip")
    def export_terminal_strip(
        self,
        path: Path | str,
        strip: str,
        vendor: str = "phoenix",
        file_format: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.io.strip_export import write_strip_export

        return write_strip_export(path, self.project, self.catalog, strip, vendor, file_format=file_format)

    @_traced_path("export_terminal_strips")
    def export_terminal_strips(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, self.terminal_strip_rows())
        return dest

    def audit_schematic_panel(self) -> list[dict[str, str]]:
        from panelcad.core.electrical import audit_schematic_panel

        return audit_schematic_panel(self.project, self.catalog)

    @_traced_path("export_audit")
    def export_audit(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, self.audit_schematic_panel())
        return dest

    def check_sizing(self) -> dict[str, Any]:
        """Motor wire, branch-device, and overload-range findings, plus a feeder when several motors are sized."""

        from panelcad.core.motor_apply import feeder_for_project
        from panelcad.core.sizing_checks import collect_sizing_issues

        findings = collect_sizing_issues(self.project, self.catalog, self.parts)
        return {
            "findings": findings,
            "count": len(findings),
            "feeder": feeder_for_project(self.project, self.catalog),
        }

    def design_checks(self, *, include_suppressed: bool = False) -> list[dict[str, str]]:
        """Live design findings. Same engine as validate, audit, CLI, and the agent."""

        from panelcad.core.checks import run_checks

        return [
            item.to_row()
            for item in run_checks(
                self.project,
                self.catalog,
                include_suppressed=include_suppressed,
                parts=self.parts,
                project_path=self.path,
            )
        ]

    def power_budget(self, *, supply: str | None = None, margin: float | None = None) -> dict[str, Any]:
        """Loads on each control supply, totals, and margin. Does not write the project."""

        from panelcad.core.power_budget import report_dict

        return report_dict(self.project, self.catalog, self.parts, supply=supply, margin=margin)

    def set_control_power_margin(self, margin: float) -> dict[str, Any]:
        """Steady-state limit as a fraction of nameplate. 0.8 is 80%."""

        from panelcad.core.power_budget import MARGIN_KEY, margin_of

        value = margin_of(self.project, margin)
        self.project.properties[MARGIN_KEY] = value
        return {"margin": value}

    def set_power_estimate(
        self,
        ref: str,
        *,
        power_w: float | None = None,
        current_a: float | None = None,
        coil_va_sealed: float | None = None,
        coil_va_inrush: float | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace catalog consumption with a labeled estimate on this tag."""

        sheets = [self.sheet(sheet_id)] if sheet_id else list(self.project.sheets)
        found = False
        for sheet in sheets:
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            comp.properties["power_origin"] = "estimate"
            comp.properties.pop("power_source", None)
            if power_w is not None:
                comp.properties["power_w"] = float(power_w)
            if current_a is not None:
                comp.properties["current_a"] = float(current_a)
            if coil_va_sealed is not None:
                comp.properties["coil_va_sealed"] = float(coil_va_sealed)
            if coil_va_inrush is not None:
                comp.properties["coil_va_inrush"] = float(coil_va_inrush)
            found = True
        if not found:
            raise KeyError(f"no component '{ref}'")
        return {"id": ref, "power_origin": "estimate", "power_w": power_w, "current_a": current_a}

    @_traced_path("export_power_budget")
    def export_power_budget(
        self,
        path: Path | str,
        *,
        supply: str | None = None,
        margin: float | None = None,
    ) -> Path:
        from panelcad.core.power_budget import analyze, csv_rows
        from panelcad.render.reports import write_csv

        dest = Path(path)
        rows = csv_rows(analyze(self.project, self.catalog, self.parts, supply=supply, margin=margin))
        write_csv(dest, rows)
        return dest

    @_traced_path("export_power_budget_pdf")
    def export_power_budget_pdf(
        self,
        path: Path | str,
        *,
        supply: str | None = None,
        margin: float | None = None,
    ) -> Path:
        from panelcad.core.power_budget import analyze
        from panelcad.render.power_report import write_power_budget_pdf

        _ensure_qt_app()
        budgets = analyze(self.project, self.catalog, self.parts, supply=supply, margin=margin)
        return write_power_budget_pdf(path, budgets, title=self.project.name)

    @_traced_path("export_checks")
    def export_checks(self, path: Path | str, *, include_suppressed: bool = False) -> Path:
        dest = Path(path)
        write_csv(dest, self.design_checks(include_suppressed=include_suppressed))
        return dest

    def compute_sccr(self) -> dict[str, Any]:
        """UL 508A SB short-circuit current rating. Does not write the drawing."""

        from panelcad.electrical.sccr import compute_sccr as rate_panel

        return rate_panel(self.project, self.catalog, self.parts).to_dict()

    def set_available_fault(
        self,
        ka: float | None = None,
        voltage: float | None = None,
        sccr_voltage: float | None = None,
    ) -> dict[str, Any]:
        """Store the available fault current, and optionally the panel SCCR voltage."""

        if ka is not None:
            self.project.properties["available_fault_ka"] = float(ka)
        if voltage is not None:
            self.project.properties["available_fault_voltage"] = float(voltage)
        if sccr_voltage is not None:
            self.project.properties["sccr_voltage"] = float(sccr_voltage)
        return self.compute_sccr()

    def set_part_sccr(self, ref: str, sccr_ka: float | None = None, **fields: Any) -> dict[str, Any]:
        """Write an SCCR figure onto every sheet that carries ``ref``."""

        allowed = {
            "sccr_ka",
            "sccr",
            "ir_ka",
            "fuse_class",
            "fuse_amps",
            "circuit",
            "current_limiting",
            "let_through_ka",
            "let_through_at_ka",
            "hp",
            "kva",
            "secondary_voltage",
            "secondary_voltage_ceiling",
            "phases",
            "impedance_pct",
            "transformer_isc_ka",
            "sccr_with_class",
            "sccr_with_max_amps",
            "ul_type",
            "gfci",
            "volts",
            "voltage",
            "amps",
            "kva",
        }
        updates: dict[str, Any] = {}
        if sccr_ka is not None:
            updates["sccr_ka"] = float(sccr_ka)
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown SCCR field '{key}'")
            if key == "sccr_ka" and value is not None:
                updates[key] = float(value)
            else:
                updates[key] = value
        found = False
        for sheet in self.project.sheets:
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            comp.properties.update(updates)
            found = True
        if not found:
            raise KeyError(f"no component '{ref}'")
        result = self.compute_sccr()
        result["ref"] = ref
        return result

    @_traced_path("export_sccr_pdf")
    def export_sccr_pdf(self, path: Path | str) -> Path:
        from panelcad.electrical.sccr import compute_sccr as rate_panel
        from panelcad.electrical.sccr_page import write_sccr_pdf

        rated = rate_panel(self.project, self.catalog, self.parts)
        return write_sccr_pdf(path, rated, self.project.name)

    @_traced_path("export_sccr_png")
    def export_sccr_png(self, path: Path | str, *, scale: float = 8.0) -> Path:
        from panelcad.electrical.sccr import compute_sccr as rate_panel
        from panelcad.electrical.sccr_page import write_sccr_png

        rated = rate_panel(self.project, self.catalog, self.parts)
        return write_sccr_png(path, rated, self.project.name, scale=scale)

    def nameplate_build(self) -> dict[str, Any]:
        """Nameplate fields, full-load working, and checks. Does not write the drawing."""

        from panelcad.electrical.nameplate import build_nameplate

        return build_nameplate(self.project, self.catalog).to_dict()

    def nameplate_set(self, **updates: Any) -> dict[str, Any]:
        """Override a nameplate field or plate option. One undo."""

        from panelcad.electrical.nameplate import apply_nameplate_update, build_nameplate

        apply_nameplate_update(self.project, updates)
        return build_nameplate(self.project, self.catalog).to_dict()

    def nameplate_export(
        self,
        pdf: Path | str | None = None,
        dxf: Path | str | None = None,
        png: Path | str | None = None,
    ) -> dict[str, Any]:
        """Write the real-size PDF, the engraving DXF, or a preview PNG."""

        from panelcad.electrical.nameplate import build_nameplate
        from panelcad.electrical.nameplate_page import (
            write_nameplate_dxf,
            write_nameplate_pdf,
            write_nameplate_png,
        )

        try:
            plate = build_nameplate(self.project, self.catalog)
            result: dict[str, Any] = {"nameplate": plate.to_dict()}
            if pdf:
                result["pdf"] = str(write_nameplate_pdf(pdf, plate))
            if dxf:
                paths = write_nameplate_dxf(dxf, plate)
                result["dxf"] = str(paths[0])
                if len(paths) > 1:
                    result["caution_dxf"] = str(paths[1])
            if png:
                result["png"] = str(write_nameplate_png(png, plate))
            if not any([pdf, dxf, png]):
                raise ValueError("nameplate_export needs pdf, dxf, or png")
        except Exception as exc:
            _log_io("error", f"nameplate_export {_safe_io_error(exc)}")
            raise
        for key in ("pdf", "dxf", "caution_dxf", "png"):
            written = result.get(key)
            if written:
                _log_io("nameplate_export", written)
        return result

    def suppress_finding(self, finding_id: str) -> dict[str, Any]:
        from panelcad.core.checks import set_suppressed_ids, suppressed_ids

        key = str(finding_id or "").strip()
        if not key:
            raise ValueError("finding id is required")
        with self.command("suppress finding"):
            ids = suppressed_ids(self.project)
            if key not in ids:
                ids.append(key)
            set_suppressed_ids(self.project, ids)
        return {"id": key, "suppressed": True, "count": len(ids)}

    def unsuppress_finding(self, finding_id: str) -> dict[str, Any]:
        from panelcad.core.checks import set_suppressed_ids, suppressed_ids

        key = str(finding_id or "").strip()
        with self.command("restore finding"):
            ids = [item for item in suppressed_ids(self.project) if item != key]
            set_suppressed_ids(self.project, ids)
        return {"id": key, "suppressed": False, "count": len(ids)}

    def io_rows(self) -> list[dict[str, str]]:
        from panelcad.core.electrical import io_rows

        return io_rows(self.project, self.catalog)

    def io_add_module(
        self,
        module: str,
        *,
        ref: str | None = None,
        slot: int = 1,
        position: int = 1,
        polarity: str = "",
        address_base: int | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.io_sheets import add_module

        return self._undoable(
            "io_add_module",
            f"add I/O module {module}",
            lambda: add_module(
                self,
                module,
                ref=ref,
                slot=slot,
                position=position,
                polarity=polarity,
                address_base=address_base,
            ),
        )

    def io_assign_point(
        self,
        module: str,
        pin: str,
        *,
        tag: str | None = None,
        description: str | None = None,
        field: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.io_sheets import assign_point

        return self._undoable(
            "io_assign_point",
            f"assign {module}.{pin}",
            lambda: assign_point(self, module, pin, tag=tag, description=description, field=field),
        )

    def io_generate_sheets(self, modules: list[str] | None = None, *, fuses: bool | None = None) -> dict[str, Any]:
        from panelcad.core.io_sheets import generate_sheets

        label = ", ".join(modules) if modules else "I/O sheets"
        return self._undoable("io_generate_sheets", label, lambda: generate_sheets(self, modules, fuses=fuses))

    def apply_io_wizard(
        self,
        specs: list[dict[str, Any]],
        assignments: list[dict[str, Any]] | None = None,
        *,
        fuses: bool | None = None,
    ) -> dict[str, Any]:
        """Add modules, assign points, and draw sheets as one undo."""

        from panelcad.core.io_sheets import add_module, assign_point, generate_sheets

        rows = list(assignments or [])

        def run() -> dict[str, Any]:
            added = []
            for spec in specs:
                added.append(
                    add_module(
                        self,
                        str(spec.get("module") or ""),
                        ref=spec.get("ref") or None,
                        slot=int(spec.get("slot") or 1),
                        position=int(spec.get("position") or 1),
                        polarity=str(spec.get("polarity") or ""),
                        address_base=spec.get("address_base"),
                    )
                )
            for row in rows:
                assign_point(
                    self,
                    str(row.get("module") or ""),
                    str(row.get("pin") or ""),
                    tag=row.get("tag"),
                    description=row.get("description"),
                    field=row.get("field"),
                )
            drawn = generate_sheets(self, fuses=fuses)
            return {"added": added, **drawn}

        return self._undoable("io_generate_sheets", "I/O schematic sheets", run)

    @_traced_path("import_io")
    def import_io_list(self, path: Path | str) -> dict[str, Any]:
        from panelcad.core.electrical import import_io_csv
        from panelcad.core.io_sheets import apply_imported_io, generate_sheets

        def run() -> dict[str, Any]:
            # Snapshot is taken by _undoable before this runs, so one undo
            # restores the drawing and the I/O map together.
            result = import_io_csv(self.project, self.catalog, path)
            apply_imported_io(self)
            try:
                drawn = generate_sheets(self)
            except ValueError:
                drawn = None
            result["sheets"] = list(drawn.get("sheets") or []) if drawn else []
            return result

        return self._undoable("import_io", "import I/O list", run)

    @_traced_path("export_io")
    def export_io_list(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, self.io_rows())
        return dest

    @_traced_path("export_plc_tags")
    def export_plc_tags(
        self,
        path: Path | str,
        *,
        target: str,
        prefix: str = "",
        name_from: str = "device",
        logix_style: str = "alias",
        logix_slot: int = 1,
        analog_start: int | None | str = "project",
    ) -> dict[str, Any]:
        """Write a PLC tag file for Studio 5000, TIA Portal, or Do-more.

        ``analog_start`` is the TIA %IW/%QW byte address when a module has no
        library start address. Omit it to use project property ``analog_start``.
        ``None`` or ``\"auto\"`` picks the next free even word after the digital bytes.
        """

        from panelcad.core.plc_tags import export_plc_tags

        return export_plc_tags(
            self.project,
            self.catalog,
            path,
            target=target,
            prefix=prefix,
            name_from=name_from,
            logix_style=logix_style,
            logix_slot=logix_slot,
            analog_start=analog_start,
        )

    def assign_item_numbers(self) -> int:
        from panelcad.core.electrical import assign_item_numbers

        return assign_item_numbers(self.project)

    def cable_rows(self) -> list[dict[str, str]]:
        from panelcad.core.electrical import cable_rows

        return cable_rows(self.project)

    def propose_cables(
        self,
        *,
        spares: int | None = None,
        replace: bool = False,
        show_tags: bool = True,
        route: str | None = None,
    ) -> dict[str, Any]:
        """Group field wires into cables. One undo."""

        from panelcad.core.cables import cable_dict, propose_cables

        def _run() -> dict[str, Any]:
            created = propose_cables(
                self.project,
                self.catalog,
                self.parts,
                spares=spares,
                replace=replace,
                show_tags=show_tags,
                route=route,
            )
            return {"cables": [cable_dict(cable) for cable in created], "count": len(created)}

        return self._undoable("propose_cables", "propose field cables", _run)

    def add_cable(self, **fields: Any) -> dict[str, Any]:
        from panelcad.core.cables import add_cable, cable_dict

        def _run() -> dict[str, Any]:
            cable = add_cable(self.project, self.parts, **fields)
            return cable_dict(cable)

        label = str(fields.get("tag") or fields.get("to_device") or "cable")
        return self._undoable("add_cable", f"add cable {label}", _run)

    def update_cable(self, tag: str, /, **fields: Any) -> dict[str, Any]:
        from panelcad.core.cables import cable_dict, update_cable

        def _run() -> dict[str, Any]:
            cable = update_cable(self.project, tag, **fields)
            return cable_dict(cable)

        return self._undoable("update_cable", f"edit cable {tag}", _run)

    def delete_cable(self, tag: str) -> dict[str, Any]:
        from panelcad.core.cables import delete_cable

        def _run() -> dict[str, Any]:
            delete_cable(self.project, tag)
            return {"tag": tag, "deleted": True}

        return self._undoable("delete_cable", f"delete cable {tag}", _run)

    def assign_cable_conductors(self, tag: str, conductors: list[dict[str, Any]]) -> dict[str, Any]:
        from panelcad.core.cables import assign_conductors, cable_dict

        def _run() -> dict[str, Any]:
            cable = assign_conductors(self.project, tag, conductors)
            return cable_dict(cable)

        return self._undoable("assign_cable_conductors", f"assign conductors on {tag}", _run)

    def remap_cable(self, tag: str, *, spares: int | None = None) -> dict[str, Any]:
        from panelcad.core.cables import cable_dict, remap_cable

        def _run() -> dict[str, Any]:
            cable = remap_cable(self.project, self.catalog, self.parts, tag, spares=spares)
            return cable_dict(cable)

        return self._undoable("remap_cable", f"map conductors on {tag}", _run)

    def list_cables(self) -> list[dict[str, Any]]:
        from panelcad.core.cables import cable_dict

        return [cable_dict(cable) for cable in self.project.cables]

    @_traced_path("export_cable_schedule")
    def export_cable_schedule(self, path: Path | str) -> Path:
        dest = Path(path)
        if dest.suffix.lower() == ".pdf":
            return self.export_cable_schedule_pdf(dest)
        from panelcad.core.cables import SCHEDULE_COLUMNS, write_rows_csv

        write_rows_csv(dest, self.cable_rows(), SCHEDULE_COLUMNS)
        return dest

    @_traced_path("export_cable_schedule_pdf")
    def export_cable_schedule_pdf(self, path: Path | str) -> Path:
        from panelcad.render.cable_schedule import write_cable_schedule_pdf

        _ensure_qt_app()
        return write_cable_schedule_pdf(self, path)

    @_traced_path("export_cable_pull_list")
    def export_cable_pull_list(self, path: Path | str) -> Path:
        from panelcad.core.cables import PULL_COLUMNS, pull_rows, write_rows_csv

        dest = Path(path)
        write_rows_csv(dest, pull_rows(self.project), PULL_COLUMNS)
        return dest

    @_traced_path("export_cable_terminations")
    def export_cable_terminations(self, path: Path | str) -> Path:
        from panelcad.core.cables import TERMINATION_COLUMNS, termination_rows, write_rows_csv

        dest = Path(path)
        write_rows_csv(dest, termination_rows(self.project), TERMINATION_COLUMNS)
        return dest

    def panel_wire_annotations(self) -> list[dict[str, str]]:
        from panelcad.core.electrical import panel_wire_annotations

        return panel_wire_annotations(self.project, self.catalog)

    @_traced_path("export_panel_wires")
    def export_panel_wires(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, self.panel_wire_annotations())
        return dest

    @_traced_path("export_bom")
    def export_bom(
        self,
        path: Path | str,
        grouped: bool = False,
        panel: str | None = None,
    ) -> Path:
        """Write the bill. ``panel`` is a panel tag, or ``project`` for the summed total."""

        from panelcad.io.bom import bom_for_panel, grouped_rows, instance_rows, with_needs_info_section, write_bom_csv

        dest = Path(path)
        scope = str(panel or "").strip()
        if scope.casefold() in {"project", "all", "*"}:
            rows = grouped_rows(self.project, self.catalog, by_panel=False)
        elif scope:
            rows = bom_for_panel(self.project, self.catalog, scope)
        elif grouped:
            rows = grouped_rows(self.project, self.catalog)
        else:
            rows = instance_rows(self.project, self.catalog)
        write_bom_csv(dest, with_needs_info_section(rows, self.project, self.catalog))
        return dest

    def quote_build(self, today: str | None = None) -> dict[str, Any]:
        """Build a quote from the drawing. Does not write a file or send anything."""

        from datetime import date

        from panelcad.quote.build import build_quote
        from panelcad.quote.prices import parse_price_date

        day = None
        if today:
            parsed = parse_price_date(today)
            day = parsed if isinstance(parsed, date) else None
        return build_quote(self.project, self.catalog, self.parts, today=day)

    def quote_set_rates(self, **fields: Any) -> dict[str, Any]:
        """Save labor rates, markups, and manual prices on this project or the user file."""

        from panelcad.quote.build import apply_rates
        from panelcad.quote.labor import EXAMPLE_LABEL, example_defaults_remaining, resolved_labor

        with self.command("quote rates"):
            apply_rates(self.project, **fields)
        table = resolved_labor(self.project)
        defaults_left = example_defaults_remaining(table)
        blob = self.project.properties.get("quote") if isinstance(self.project.properties.get("quote"), dict) else {}
        return {
            "sent": False,
            "label": EXAMPLE_LABEL if defaults_left else "",
            "example_defaults": bool(defaults_left),
            "scope": str(fields.get("scope") or "project"),
            "number": str(blob.get("number") or ""),
            "customer": str(blob.get("customer") or self.project.title_block.customer or ""),
            "project": str(blob.get("project") or self.project.name or ""),
            "price_mode": str(blob.get("price_mode") or "cheapest"),
            "preferred_vendor": str(blob.get("preferred_vendor") or ""),
            "parts_markup_percent": float(blob.get("parts_markup_percent") or 0),
            "labor_markup_percent": float(blob.get("labor_markup_percent") or 0),
            "tax_percent": float(blob.get("tax_percent") or 0),
            "freight": float(blob.get("freight") or 0),
            "contingency_percent": float(blob.get("contingency_percent") or 0),
            "stale_after_days": int(table.get("stale_after_days") or 365),
            "currency": str(blob.get("currency") or ""),
            "labor": table,
        }

    @_traced_path("quote_import_prices")
    def quote_import_prices(
        self,
        path: Path | str,
        *,
        price_mode: str | None = None,
        preferred_vendor: str | None = None,
    ) -> dict[str, Any]:
        """Replace this project's price list with a CSV or XLSX the user already has."""

        from panelcad.quote.build import ensure_quote
        from panelcad.quote.prices import read_price_list

        if price_mode and price_mode not in {"cheapest", "preferred"}:
            raise ValueError("price_mode must be cheapest or preferred")
        rows, skipped, encoding = read_price_list(path)
        with self.command("import prices"):
            blob = ensure_quote(self.project)
            blob["prices"] = rows
            if price_mode:
                blob["price_mode"] = price_mode
            if preferred_vendor is not None:
                blob["preferred_vendor"] = str(preferred_vendor)
        vendors = sorted({str(row.get("vendor") or "") for row in rows if str(row.get("vendor") or "").strip()})
        return {
            "path": str(path),
            "rows": len(rows),
            "vendors": vendors,
            "skipped": skipped,
            "encoding": encoding,
            "sent": False,
        }

    @_traced_path("quote_export")
    def quote_export(
        self,
        path: Path | str,
        *,
        fmt: str | None = None,
        view: str = "internal",
        today: str | None = None,
    ) -> dict[str, Any]:
        """Write the quote to a file. Customer view omits cost and markup. Nothing is sent."""

        from datetime import date

        from panelcad.core.checks import require_layout_ready_for_build
        from panelcad.quote.export import export_quote
        from panelcad.quote.prices import parse_price_date

        require_layout_ready_for_build(self.project, self.catalog)
        day = None
        if today:
            parsed = parse_price_date(today)
            day = parsed if isinstance(parsed, date) else None
        return export_quote(
            self.project,
            self.catalog,
            path,
            parts=self.parts,
            fmt=fmt,
            view=view,
            today=day,
        )

    @_traced_path("import_bom")
    def import_bom(self, path: Path | str, *, place_missing: bool = True) -> dict[str, Any]:
        from panelcad.io.bom import apply_bom_rows, read_bom_csv

        rows = read_bom_csv(path)
        result = apply_bom_rows(self, rows, place_missing=place_missing)
        return result.as_dict()

    @_traced_path("export_wire_list")
    def export_wire_list(self, path: Path | str) -> Path:
        dest = Path(path)
        write_csv(dest, wire_rows(self.project))
        return dest

    def from_to_rows(self, sort: str = "number") -> list[dict[str, str]]:
        """One row per conductor. Does not assign wire numbers."""

        from panelcad.core.from_to import from_to_rows

        return from_to_rows(self.project, self.catalog, sort=sort)

    @_traced_path("export_from_to")
    def export_from_to(self, path: Path | str, sort: str = "number") -> Path:
        from panelcad.core.from_to import export_from_to

        return export_from_to(self.project, self.catalog, path, sort=sort)

    def set_net_wire_spec(
        self,
        net: str,
        *,
        size: str | None = None,
        color: str | None = None,
        wire_type: str | None = None,
    ) -> dict[str, Any]:
        """Default size, color, and type for one net. ``net='*'`` or ``all`` is the fallback.

        A blank field on the wire inherits this. Empty string clears that field.
        Clearing every field removes the net entry. ``all`` is stored as ``*``.
        """

        from panelcad.core.models import NetWireSpec

        key = str(net or "").strip()
        if key.lower() == "all":
            key = "*"
        if not key:
            raise ValueError("net name is required ('*' or 'all' is the fallback for every net)")
        current = self.project.net_wire_specs.get(key) or NetWireSpec()
        if size is not None:
            current.size = str(size).strip()
        if color is not None:
            current.color = str(color).strip()
        if wire_type is not None:
            current.wire_type = str(wire_type).strip()
        if not (current.size or current.color or current.wire_type):
            self.project.net_wire_specs.pop(key, None)
            return {"net": key, "size": "", "color": "", "type": "", "cleared": True}
        self.project.net_wire_specs[key] = current
        return {
            "net": key,
            "size": current.size,
            "color": current.color,
            "type": current.wire_type,
            "cleared": False,
        }

    def wire_marker_rows(self, *, style: str = "heat-shrink", sort: str = "number") -> list[dict[str, str]]:
        from panelcad.io.wire_markers import marker_rows

        rows, _blank = marker_rows(self.project, self.catalog, style=style, sort=sort)
        return rows

    @_traced_path("export_wire_markers")
    def export_wire_markers(
        self,
        path: Path | str,
        *,
        target: str = "generic",
        style: str = "heat-shrink",
        sort: str = "number",
    ) -> dict[str, Any]:
        from panelcad.io.wire_markers import export_wire_markers

        return export_wire_markers(
            self.project,
            self.catalog,
            path,
            target=target,
            style=style,
            sort=sort,
        )

    @_traced_path("export_json")
    def export_json(self, path: Path | str) -> Path:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(self.project.model_dump(mode="json", by_alias=True), indent=2),
            encoding="utf-8",
        )
        return dest

    @_traced_path("export_package")
    def export_package(
        self,
        dest: Path | str,
        *,
        pdf: bool = True,
        paper: str = "mixed",
        sections: list[str] | None = None,
    ) -> Path:
        from panelcad.io.exchange import export_package

        return export_package(self, dest, pdf=pdf, paper=paper, sections=sections)

    def plan_print_package(
        self,
        *,
        sections: str | list[str] | None = None,
        paper: str = "mixed",
    ) -> dict[str, Any]:
        """Page list for the one-click print package (no file written)."""

        from panelcad.render.print_package import plan_print_package

        return plan_print_package(self, sections=sections, paper=paper).as_dict()

    @_traced_path("print_package")
    def print_package(
        self,
        path: Path | str,
        *,
        sections: str | list[str] | None = None,
        paper: str = "mixed",
    ) -> dict[str, Any]:
        """One PDF: cover, index, revisions, drawings, and paginated reports."""

        from panelcad.core.checks import require_layout_ready_for_build

        # INFO NEEDED markers may print, stamped NOT FOR BUILD. A real generic
        # or other unconfirmed part still blocks the package.
        require_layout_ready_for_build(
            self.project, self.catalog, allow_needs_info_placeholders=True
        )
        from panelcad.render.print_package import write_print_package
        _ensure_qt_app()
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return write_print_package(self, dest, sections=sections, paper=paper)

    def import_print(
        self,
        path: Path | str,
        *,
        workspace: Path | str | None = None,
        vision_backend: str = "none",
        vision_model: str = "",
        vision_base_url: str = "",
        vision_api_key_env: str = "",
        vision_workers: int | None = None,
        vision_timeout_s: float | None = None,
        vision_page_timeout_s: float | None = None,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        think: bool | None = None,
        reasoning_effort: str = "",
        dpi: float | None = None,
        truth: str | Path | None = None,
        out: Path | str | None = None,
    ) -> dict[str, Any]:
        """Import a print into this session. Timeouts are per request and per page."""

        from panelcad.importer.pipeline import ImportJob
        from panelcad.importer.vision_config import vision_from_options

        settings = vision_from_options(
            preset=vision_backend or "none",
            base_url=vision_base_url,
            model=vision_model,
            api_key_env=vision_api_key_env,
            workers=vision_workers,
            timeout_s=vision_timeout_s,
            page_timeout_s=vision_page_timeout_s,
            num_ctx=num_ctx,
            num_predict=num_predict,
            think=think,
            reasoning_effort=reasoning_effort,
        )
        root = workspace or (Path(out).parent / "import-workspace" if out else Path("import-workspace"))
        backend = "raster" if settings.preset == "none" else settings.preset
        job = ImportJob.start(
            [path],
            root,
            backend=backend,
            model=vision_model or None,
            vision=settings,
            dpi=dpi,
            truth=truth,
        )
        built = job.build(out=out)
        self.project = built.project
        self.catalog = built.catalog
        self.path = built.path
        self._invalidate_sheet_cache()
        return job.status()

    def import_diagnostics(self, run: str | Path | None = None, *, truth: str | Path | None = None) -> dict[str, Any]:
        """Funnel for a print-import run. ``run`` may be a directory, a project, or a run id."""

        from panelcad.importer.diagnostics import load_run

        info = self.project.properties.get("print_import")
        return load_run(
            run,
            truth=truth,
            project_path=self.path,
            properties=info if isinstance(info, dict) else None,
        )

    @_traced_path("import")
    def import_any(self, path: Path | str, *, replace: bool = False) -> dict[str, Any]:
        from panelcad.io.exchange import import_any

        return import_any(self, path, replace=replace)

    @_traced_path("import_library")
    def import_library(self, path: Path | str) -> list[str]:
        from panelcad.io.exchange import import_library

        return import_library(self, path)

    def _library_write_path(self, library_id: str) -> Path:
        from panelcad.library.paths import is_builtin_path, user_library_dir

        existing = self.catalog.source_path(library_id)
        if existing is not None and not is_builtin_path(existing):
            return existing
        if self.path is not None:
            return self.path.parent / "library" / "user.yaml"
        return user_library_dir() / "user.yaml"

    def save_library_device(self, spec: dict[str, Any] | Device) -> dict[str, Any]:
        """Add or replace a catalog object. Builtin YAML is never overwritten."""

        from panelcad.library.catalog import _device_from_raw

        if isinstance(spec, Device):
            device = spec
            if "." not in device.library_id:
                device.library_id = f"local.{device.id}"
        else:
            spec = dict(spec)
            source_id = str(spec.get("library_id") or spec.get("source") or "")
            short = str(spec.get("id") or spec.get("new_id") or "").strip()
            if source_id and source_id in self.catalog.devices:
                device = self.catalog.get(source_id).model_copy(deep=True)
                if short:
                    device.id = short
                device.library_id = f"local.{device.id}"
                if spec.get("name"):
                    device.name = str(spec["name"])
                if spec.get("category"):
                    device.category = str(spec["category"])
                if spec.get("ref_prefix"):
                    device.ref_prefix = str(spec["ref_prefix"])
                if "pins" in spec:
                    device.pins = [PinDef.model_validate(p) for p in spec["pins"]]
                if spec.get("width") is not None:
                    device.schematic_width = float(spec["width"])
                if spec.get("height") is not None:
                    device.schematic_height = float(spec["height"])
                if "draw" in spec:
                    from panelcad.core.primitives import expand_draw_item

                    device.primitives = [expand_draw_item(item) for item in spec["draw"] or []]
                from panelcad.core.block import apply_symbol_spec

                apply_symbol_spec(device, spec)
            else:
                if not short:
                    raise ValueError("id required for a new library device")
                panel = dict(spec.get("panel") or {})
                if spec.get("footprint_draw"):
                    panel["draw"] = spec.get("footprint_draw")
                if spec.get("footprint_attributes"):
                    panel["attributes"] = spec.get("footprint_attributes")
                schematic = {
                    "width": spec.get("width", 24),
                    "height": spec.get("height", 24),
                    "pins": spec.get("pins") or [],
                    "draw": spec.get("draw") or [],
                }
                if spec.get("attributes"):
                    schematic["attributes"] = spec.get("attributes")
                raw = {
                    "id": short,
                    "name": spec.get("name") or short,
                    "category": spec.get("category") or "misc",
                    "ref_prefix": spec.get("ref_prefix") or "A",
                    "tags": spec.get("tags") or [],
                    "schematic": schematic,
                    "panel": panel,
                    "props": spec.get("props") or spec.get("properties") or {},
                }
                if spec.get("block") or spec.get("block_kind"):
                    from panelcad.core.block import normalize_kind

                    raw["block"] = normalize_kind(str(spec.get("block") or spec.get("block_kind")))
                device = _device_from_raw("local", raw)
        dest = self._library_write_path(device.library_id)
        path = self.catalog.save_device(device, dest)
        self._invalidate_sheet_cache()
        _log_io("save_library", path)
        self.emit(
            "save_library_device",
            f"library {device.library_id}",
            extra={"library_id": device.library_id, "path": str(path)},
        )
        return {"library_id": device.library_id, "path": str(path), "name": device.name}

    @_traced_path("import_wires")
    def import_wires(self, path: Path | str) -> dict[str, Any]:
        from panelcad.io.exchange import import_wires

        return import_wires(self, path)

    @_traced_path("import_sheet")
    def import_sheet(self, path: Path | str) -> str:
        from panelcad.io.exchange import import_sheet

        return import_sheet(self, path)

    @classmethod
    def from_any(cls, path: Path | str) -> Session:
        from panelcad.io.exchange import PROJECT_YAML, import_any, import_package, sniff

        p = Path(path)
        if p.is_dir():
            s = cls.create("Imported")
            import_package(s, p, replace=True)
            yaml_path = p / PROJECT_YAML
            s.path = yaml_path if yaml_path.is_file() else None
            return s
        kind = sniff(p)
        if kind == "project":
            return cls.open(p)
        if kind == "project_json":
            project = Project.model_validate(json.loads(p.read_text(encoding="utf-8")))
            return cls(project, path=p)
        s = cls.create("Imported")
        import_any(s, p, replace=False)
        return s

    def list_library(self, query: str = "") -> list[str]:
        return [
            d.library_id
            for d in self.catalog.search(query)
            if d.library_id not in {"reconstruct.needs_info", "reconstruct.measured_envelope"}
        ]

    def list_sheets(self) -> list[dict[str, Any]]:
        total = len(self.project.sheets)
        return [
            {
                "index": i + 1,
                "count": total,
                "id": s.id,
                "title": s.title,
                "kind": s.kind,
                "description": s.description,
                "active": s.id == self.active_sheet_id,
                "closed": s.closed,
                "components": len(s.components),
                "wires": len(s.wires),
                "rails": len(s.rails),
            }
            for i, s in enumerate(self.project.sheets)
        ]

    def list_components(self, sheet_id: str | None = None) -> list[dict[str, Any]]:
        sheet = self.sheet(sheet_id)
        rows: list[dict[str, Any]] = []
        for c in sheet.components:
            device = self.catalog.get(c.library_id)
            rows.append(
                {
                    "id": c.id,
                    "library_id": c.library_id,
                    "name": device.name,
                    "x": c.x,
                    "y": c.y,
                    "rotation": c.rotation,
                    "scale": c.visual_scale(),
                    "pins": [p.id for p in device.pins],
                    "pins_locked": c.pins_locked,
                    "pin_overrides": {k: v.model_dump() for k, v in c.pin_overrides.items()},
                    "properties": {**device.properties, **c.properties},
                    "panel": effective_footprint(device, c).model_dump(),
                    "ports": [p.model_dump() for p in effective_ports(device, c)],
                    "ip": str(c.properties.get("ip") or c.properties.get("address") or ""),
                    "node": str(c.properties.get("node") or c.properties.get("station") or ""),
                    "address": network_address(c),
                }
            )
            if self.project.panels:
                from panelcad.core.panels import component_panel_tag

                tag = component_panel_tag(self.project, sheet, c)
                if tag:
                    rows[-1]["location_panel"] = tag
        return rows

    def list_wires(self, sheet_id: str | None = None) -> list[dict[str, Any]]:
        sheet = self.sheet(sheet_id)
        return [
            {
                "id": w.id,
                "from": w.frm,
                "to": w.to,
                "net": w.net,
                "label": w.label,
                "number": w.number,
                "number_locked": bool(w.number_locked),
                "color": w.color,
                "gauge": w.gauge,
                "type": w.wire_type,
                "cable": w.cable,
                "caption": w.caption(),
            }
            for w in sheet.wires
        ]

    def inspect_device(self, library_id: str) -> dict[str, Any]:
        d = self.catalog.get(library_id)
        return {
            "library_id": d.library_id,
            "name": d.name,
            "category": d.category,
            "standard": d.standard,
            "ref_prefix": self.ref_prefix_for(d.library_id),
            "library_ref_prefix": d.ref_prefix,
            "pins": [{"id": p.id, "x": p.x, "y": p.y, "side": p.side} for p in d.pins],
            "panel": d.panel.model_dump(),
            "ports": [p.model_dump() for p in d.ports],
            "properties": dict(d.properties),
        }

    def search_parts(
        self,
        query: str = "",
        *,
        manufacturer: str | None = None,
        category: str | None = None,
        library_id: str | None = None,
        amps_min: float | None = None,
        amps_max: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search verified manufacturer catalog parts. Does not change the drawing."""

        return [
            {
                "manufacturer": part.manufacturer,
                "catalog": part.catalog,
                "library_id": part.library_id,
                "category": part.category,
                "description": part.description,
                "verified": part.verified,
                "source": part.source,
                "ratings": part.ratings_text(),
                "amps": part.amps,
                "amps_min": part.amps_min,
                "amps_max": part.amps_max,
                "poles": part.poles,
                "coil_voltage": part.coil_voltage,
                "contact_config": part.contact_config,
                "ka": part.ka,
                "sccr": part.sccr,
                "sccr_ka": part.sccr_ka,
                "heat_w": part.heat_w,
                "width_mm": part.width_mm,
                "height_mm": part.height_mm,
                "depth_mm": part.depth_mm,
                "mounting": part.mounting,
                "series": part.series,
            }
            for part in self.parts.search(
                query,
                manufacturer=manufacturer,
                category=category,
                library_id=library_id,
                amps_min=amps_min,
                amps_max=amps_max,
            )
        ]

    def assign_part(
        self,
        ref: str,
        catalog: str,
        manufacturer: str | None = None,
        sheet_id: str | None = None,  # kept for JSONL/CLI; every sheet with this tag is updated
    ) -> dict[str, Any]:
        """Write a verified catalog SKU onto every sheet that has ``ref``.

        Keeps ``library_id``, tag, and wiring. One undo via ``_ACTIVITY_METHODS``.
        Mutates instance properties directly — do not call ``set_panel``.
        """

        from panelcad.library.parts import ASSIGNED_KEYS

        _ = sheet_id
        part = self.parts.get(catalog, manufacturer)
        first = None
        for sheet in self.project.sheets:
            try:
                first = sheet.component(ref)
                break
            except KeyError:
                continue
        if first is None:
            raise KeyError(f"no component '{ref}'")
        device = self.catalog.get(first.library_id)
        if not catalog_row_fits_device(device, part):
            raise LayoutNotReadyError(
                f"{part.catalog} ({part.library_id}/{part.category}) does not fit "
                f"{first.library_id} ({device.category})"
            )
        props = part.assignment_properties()
        from panelcad.core.cutout import catalog_cutout

        published = catalog_cutout(part.manufacturer, part.catalog)
        if published is not None:
            props["cutout"] = published.to_dict()
        preserve = assignment_preserve_instance_keys(part)
        from panelcad.core.catalog_match import remember_device_rating

        updated: list[str] = []
        first = None
        for sheet in self.project.sheets:
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            wipe_confirmation = assignment_clears_confirmation(part, comp.properties)
            stale_catalog_size = str(comp.properties.get("dim_source") or "") == "catalog"
            try:
                comp_device = self.catalog.get(comp.library_id)
            except KeyError:
                comp_device = device
            remember_device_rating(comp, comp_device, part)
            for key in ASSIGNED_KEYS:
                if key in preserve and not stale_catalog_size:
                    continue
                comp.properties.pop(key, None)
            comp.properties.update(props)
            if wipe_confirmation:
                clear_envelope_confirmation(comp)
            updated.append(sheet.id)
            if first is None:
                first = comp
        if first is None:
            raise KeyError(f"no component '{ref}'")
        if part.has_footprint() and part.depth_mm is None:
            from panelcad.library.panel import CATALOG_DEPTH_MISSING_KEY

            for sheet in self.project.sheets:
                try:
                    sheet.component(ref).properties[CATALOG_DEPTH_MISSING_KEY] = True
                except KeyError:
                    continue
        from panelcad.core.circuit_layout import mount_catalog_aux

        mounted = mount_catalog_aux(self, ref)
        device = self.catalog.get(first.library_id)
        row = {
            "id": ref,
            "manufacturer": part.manufacturer,
            "catalog": part.catalog,
            "verified": part.verified,
            "source": part.source,
            "library_id": first.library_id,
            "sheets": updated,
            "panel": effective_footprint(device, first).model_dump(),
            "heat_w": part.heat_w,
            "mounted_aux": mounted,
        }
        return row

    def try_assign_sku(
        self,
        ref: str,
        catalog: str,
        manufacturer: str | None = None,
    ) -> dict[str, Any] | None:
        """``assign_part`` when the SKU exists and fits the symbol. Else ``None``."""

        text = str(catalog or "").strip()
        if not text:
            return None
        maker = str(manufacturer or "").strip() or None
        try:
            part = self.parts.get(text, maker)
        except KeyError:
            return None
        try:
            symbol = self.catalog.get(self.sheet().component(ref).library_id)
        except KeyError:
            symbol = None
            for sheet in self.project.sheets:
                try:
                    symbol = self.catalog.get(sheet.component(ref).library_id)
                    break
                except KeyError:
                    continue
        if symbol is None or not catalog_row_fits_device(symbol, part):
            return None
        try:
            return self.assign_part(ref, part.catalog, part.manufacturer)
        except (KeyError, ValueError):
            return None

    def drop_stale_assignment(self, ref: str) -> bool:
        """Drop ``catalog_key`` when labels no longer name the assigned row."""

        dropped = False
        for sheet in self.project.sheets:
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            if assignment_labels_match(comp.properties, self.parts):
                continue
            for trust in ("catalog_key", "catalog_verified", "catalog_source"):
                if trust in comp.properties:
                    dropped = True
                comp.properties.pop(trust, None)
            if str(comp.properties.get("dim_source") or "") == "catalog":
                comp.properties.pop("dim_source", None)
        return dropped

    def set_panel(
        self,
        ref: str,
        *,
        width_mm: float | None = None,
        height_mm: float | None = None,
        depth_mm: float | None = None,
        din_rail: bool | None = None,
        in_panel: bool | None = None,
        incabinet: bool | None = None,
        mount: str | None = None,
        dim_source: str = "",
        confirm: bool = False,
        confirmed_by: str = "",
        confirmed_at: str = "",
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Instance OEM / measured footprint. Used when an agent pulls catalog dimensions.

        ``confirm=true`` stores a person-confirmed envelope (measured W/H/D/mount)
        and who/when. Do not invent millimetres: only write sizes the user gave.
        """
        if sheet_id:
            comp = self.sheet(sheet_id).component(ref)
        else:
            try:
                comp = self.sheet().component(ref)
            except KeyError:
                comp = None
                for sheet in self.project.sheets:
                    try:
                        comp = sheet.component(ref)
                        break
                    except KeyError:
                        continue
                if comp is None:
                    raise KeyError(f"no component '{ref}'") from None
        if confirm:
            from panelcad.library.panel import require_explicit_envelope
            from panelcad.reconstruct.layout import mark_confirmed_measured_description

            require_explicit_envelope(width_mm, height_mm, depth_mm, mount)
            device = self.catalog.get(comp.library_id)
            envelope = apply_confirmed_envelope(
                comp,
                width_mm=float(width_mm),
                height_mm=float(height_mm),
                depth_mm=float(depth_mm),
                mount=str(mount),
                din_rail=din_rail,
                in_panel=incabinet if incabinet is not None else in_panel,
                confirmed_by=confirmed_by or str(self.project.title_block.drawn_by or "user"),
                confirmed_at=confirmed_at,
            )
            # Same tag on every sheet (schematic + layout) shares the envelope.
            mark_confirmed_measured_description(
                comp, float(width_mm), float(height_mm), float(depth_mm)
            )
            if "description" in (comp.properties or {}):
                envelope["description"] = comp.properties["description"]
            for sheet in self.project.sheets:
                try:
                    other = sheet.component(ref)
                except KeyError:
                    continue
                if other is comp:
                    continue
                other.properties.update(envelope)
                mark_confirmed_measured_description(
                    other, float(width_mm), float(height_mm), float(depth_mm)
                )
            return {"id": comp.id, "panel": effective_footprint(device, comp).model_dump(), "confirmed": True}
        refuse_nonfinite_sizes(
            {
                "width_mm": width_mm,
                "height_mm": height_mm,
                "depth_mm": depth_mm,
            }
        )
        edit = {
            key: value
            for key, value in (
                ("width_mm", width_mm),
                ("height_mm", height_mm),
                ("depth_mm", depth_mm),
                ("mount", mount),
            )
            if value is not None
        }
        if envelope_edit_changes(comp.properties, edit):
            from panelcad.reconstruct.layout import drop_envelope_confirmation

            drop_envelope_confirmation(
                comp,
                was=(
                    comp.properties.get("width_mm"),
                    comp.properties.get("height_mm"),
                    comp.properties.get("depth_mm"),
                ),
            )
        if width_mm is not None:
            comp.properties["width_mm"] = float(width_mm)
        if height_mm is not None:
            comp.properties["height_mm"] = float(height_mm)
        if depth_mm is not None:
            comp.properties["depth_mm"] = float(depth_mm)
        if din_rail is not None:
            comp.properties["din_rail"] = bool(din_rail)
        cabinet = incabinet if incabinet is not None else in_panel
        if cabinet is not None:
            flag = bool(cabinet)
            comp.properties["in_panel"] = flag
            comp.properties["incabinet"] = flag
        if mount is not None:
            comp.properties["mount"] = str(mount)
        source = str(dim_source or "").strip()
        if source == "catalog" and not assigned_catalog_ready(comp, parts=self.parts):
            # Instance dim_source=catalog is not a verified SKU. Keep measured.
            if width_mm is not None or height_mm is not None or depth_mm is not None:
                comp.properties["dim_source"] = "measured"
        elif source in ("generic", "catalog", "measured"):
            comp.properties["dim_source"] = source
        device = self.catalog.get(comp.library_id)
        return {"id": comp.id, "panel": effective_footprint(device, comp).model_dump()}

    def confirm_envelope(
        self,
        ref: str,
        *,
        width_mm: float,
        height_mm: float,
        depth_mm: float,
        mount: str,
        din_rail: bool | None = None,
        in_panel: bool | None = None,
        confirmed_by: str = "",
        confirmed_at: str = "",
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Confirm a measured envelope for ``ref`` on every sheet that has that tag."""

        return self.set_panel(
            ref,
            width_mm=width_mm,
            height_mm=height_mm,
            depth_mm=depth_mm,
            din_rail=din_rail,
            in_panel=in_panel,
            mount=mount,
            dim_source="measured",
            confirm=True,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            sheet_id=sheet_id,
        )

    def get_title_block(self) -> dict[str, Any]:
        return self.project.title_block.model_dump()

    def ask_print_style(self) -> dict[str, Any]:
        """Ask which schematic print style to use. Panel/network pages ignore it."""

        from panelcad.core.models import PRINT_STYLES, normalize_drawing_style

        current = normalize_drawing_style(self.project.title_block.drawing_style, default="")
        options = [
            {"id": sid, "name": name, "hint": hint} for sid, name, hint in PRINT_STYLES
        ]
        payload = {
            "drawing_style": current,
            "applies_to": "schematic sheets only (not panel_layout or network)",
            "options": options,
            "set_with": {
                "op": "set_title_block",
                "drawing_style": "schematic|ladder|freeform",
            },
        }
        if current in {"ladder", "schematic", "freeform"}:
            payload["pending"] = False
            payload["asked"] = False
            return payload
        payload["pending"] = True
        payload["asked"] = True
        payload["question"] = (
            "Which print style for schematic sheets? "
            "schematic (left-to-right), ladder (rungs L1–L2), or freeform (place anywhere). "
            "Does not change panel layout or network pages."
        )
        return payload

    def move(
        self,
        ref: str,
        x: float,
        y: float,
        sheet_id: str | None = None,
        *,
        snap: bool = True,
        rail_id: str | None = None,
    ) -> Component:
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        px, py = float(x), float(y)
        device = self.catalog.get(comp.library_id)
        if snap and is_layout_sheet(sheet) and effective_footprint(device, comp).din_rail and sheet.rails:
            rail = sheet.rail(rail_id) if rail_id else nearest_rail(sheet, py)
            if rail is not None:
                px, py = place_on_rail(device, rail, px, component=comp)
        comp.x = px
        comp.y = py
        return comp

    def lift_symbols_off_title_block(
        self,
        refs: list[str] | tuple[str, ...] | set[str] | None = None,
        sheet_id: str | None = None,
    ) -> list[str]:
        """Lift only the named schematic symbols off the title block.

        An empty list lifts nothing. Symbols already on the title block stay
        where they are and show up as ``symbol_on_title_block``.
        """

        sheet = self.sheet(sheet_id)
        if sheet.kind not in {"schematic", "io"} or not refs:
            return []
        wanted = {str(ref) for ref in refs}
        moved: list[str] = []
        for comp in sheet.components:
            if comp.id not in wanted:
                continue
            try:
                device = self.catalog.get(comp.library_id)
            except KeyError:
                continue
            body_w, body_h = symbol_body_size(sheet, device, comp)
            nx, ny = lift_off_title_block(sheet, comp.x, comp.y, body_w, body_h)
            if abs(nx - comp.x) > 0.05 or abs(ny - comp.y) > 0.05:
                comp.x, comp.y = nx, ny
                moved.append(comp.id)
        return moved

    def set_scale(self, ref: str, scale: float, sheet_id: str | None = None) -> Component:
        comp = self.sheet(sheet_id).component(ref)
        try:
            s = float(scale)
        except (TypeError, ValueError):
            s = 1.0
        if s <= 0:
            s = 1.0
        comp.scale = max(0.15, min(8.0, s))
        return comp

    def set_display_size(
        self,
        ref: str,
        width_mm: float,
        height_mm: float,
        sheet_id: str | None = None,
    ) -> Component:
        """Drawn footprint on panel/door sheets. Catalog W/H/D (BOM) is unchanged."""
        comp = self.sheet(sheet_id).component(ref)
        w = max(1.0, float(width_mm))
        h = max(1.0, float(height_mm))
        comp.properties["display_width_mm"] = w
        comp.properties["display_height_mm"] = h
        return comp

    def set_pins_locked(self, ref: str, locked: bool = True, sheet_id: str | None = None) -> Component:
        """Lock (default) or unlock connection points so they can be slid on the symbol."""
        comp = self.sheet(sheet_id).component(ref)
        comp.pins_locked = bool(locked)
        return comp

    def set_pin(
        self,
        ref: str,
        pin_id: str,
        x: float,
        y: float,
        side: str | None = None,
        sheet_id: str | None = None,
    ) -> PinOverride:
        """Move a connection point on this instance. Unlock first in the GUI."""
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        device = self.catalog.get(comp.library_id)
        ports = effective_ports(device, comp)
        known = {p.id for p in device.pins} | {p.id for p in ports}
        if pin_id not in known:
            raise KeyError(f"{ref} has no pin '{pin_id}'")
        from panelcad.render.io_ladder import io_card_size, is_io_module
        from panelcad.render.network import node_size

        if is_io_sheet(sheet) and is_io_module(device):
            w, h = io_card_size(device)
        elif is_network_sheet(sheet):
            w, h = node_size(len(ports))
        else:
            w, h = device.schematic_width, device.schematic_height
        sid = side or infer_pin_side(float(x), float(y), w, h)
        if sid not in ("left", "right", "top", "bottom"):
            sid = "left"
        ov = PinOverride(x=float(x), y=float(y), side=sid)  # type: ignore[arg-type]
        comp.pin_overrides[pin_id] = ov
        return ov

    def reset_pins(self, ref: str, pin_id: str | None = None, sheet_id: str | None = None) -> Component:
        """Restore library (or auto-layout) pin positions."""
        comp = self.sheet(sheet_id).component(ref)
        if pin_id:
            comp.pin_overrides.pop(str(pin_id), None)
        else:
            comp.pin_overrides.clear()
        return comp

    def add_rail(
        self,
        rail_id: str,
        x: float,
        y: float,
        length_mm: float = 400.0,
        sheet_id: str | None = None,
    ) -> Rail:
        sheet = self.sheet(sheet_id)
        rid = _slug(rail_id)
        if any(r.id == rid for r in sheet.rails):
            raise ValueError(f"rail '{rid}' already exists on {sheet.id}")
        rail = Rail(id=rid, x=float(x), y=float(y), length_mm=float(length_mm))
        sheet.rails.append(rail)
        return rail

    def delete_rail(self, rail_id: str, sheet_id: str | None = None) -> None:
        sheet = self.sheet(sheet_id)
        before = len(sheet.rails)
        sheet.rails = [r for r in sheet.rails if r.id != rail_id]
        if len(sheet.rails) == before:
            raise KeyError(f"no rail '{rail_id}' on sheet '{sheet.id}'")

    def list_rails(self, sheet_id: str | None = None) -> list[dict[str, Any]]:
        sheet = self.sheet(sheet_id)
        return [r.model_dump() for r in sheet.rails]

    def _owned_backpanel(self, sheet: Sheet):
        """The panel that owns this backpanel, or None for a legacy layout sheet."""

        from panelcad.core.panels import find_panel

        if sheet.kind != "panel_layout":
            return None
        return find_panel(self.project, sheet.panel)

    def _set_owned_enclosure(
        self,
        sheet: Sheet,
        panel: Any,
        *,
        width_mm: float | None,
        height_mm: float | None,
        depth_mm: float | None,
        x: float | None,
        y: float | None,
        name: str | None,
        nema_type: str | None,
        material: str | None,
        catalog: str | None,
        manufacturer: str | None,
        mount: str | None,
        panel_width_mm: float | None,
        panel_height_mm: float | None,
        enclosure_id: str | None,
    ) -> Enclosure:
        """A panel backpanel takes a catalog enclosure id, never typed millimetres."""

        from panelcad.core.panels import lookup_box, panel_tag
        from panelcad.library.enclosures import load_enclosures

        typed = [
            ("width_mm", width_mm),
            ("height_mm", height_mm),
            ("depth_mm", depth_mm),
            ("panel_width_mm", panel_width_mm),
            ("panel_height_mm", panel_height_mm),
        ]
        given = [label for label, value in typed if value is not None]
        if given:
            raise ValueError(
                f"{sheet.id} belongs to {panel_tag(panel)}. "
                "Pass an enclosure id from libraries/enclosures.yaml. "
                f"Typed millimetres ({', '.join(given)}) are refused."
            )
        token = str(enclosure_id or catalog or "").strip()
        if not token and name:
            boxes = load_enclosures(self.path.parent if self.path else None).boxes
            if lookup_box(boxes, str(name)) is not None:
                token = str(name).strip()
        identity = [nema_type, material, manufacturer, mount, name]
        if any(value is not None for value in identity) and not token:
            raise ValueError(
                f"{sheet.id} belongs to {panel_tag(panel)}. "
                "Pass an enclosure id from libraries/enclosures.yaml."
            )
        if token:
            self._apply_panel_enclosure(panel, token, replace=not sheet.components)
        enc = sheet.enclosure or Enclosure()
        if x is not None:
            enc.x = float(x)
        if y is not None:
            enc.y = float(y)
        sheet.enclosure = enc
        return enc

    def set_enclosure(
        self,
        width_mm: float | None = None,
        height_mm: float | None = None,
        depth_mm: float | None = None,
        x: float | None = None,
        y: float | None = None,
        name: str | None = None,
        sheet_id: str | None = None,
        nema_type: str | None = None,
        material: str | None = None,
        catalog: str | None = None,
        manufacturer: str | None = None,
        mount: str | None = None,
        panel_width_mm: float | None = None,
        panel_height_mm: float | None = None,
        enclosure_id: str | None = None,
    ) -> Enclosure:
        sheet = self.sheet(sheet_id)
        owned = self._owned_backpanel(sheet)
        if owned is not None:
            return self._set_owned_enclosure(
                sheet,
                owned,
                width_mm=width_mm,
                height_mm=height_mm,
                depth_mm=depth_mm,
                x=x,
                y=y,
                name=name,
                nema_type=nema_type,
                material=material,
                catalog=catalog,
                manufacturer=manufacturer,
                mount=mount,
                panel_width_mm=panel_width_mm,
                panel_height_mm=panel_height_mm,
                enclosure_id=enclosure_id,
            )
        return self._write_enclosure(
            sheet,
            width_mm=width_mm,
            height_mm=height_mm,
            depth_mm=depth_mm,
            x=x,
            y=y,
            name=name,
            nema_type=nema_type,
            material=material,
            catalog=catalog,
            manufacturer=manufacturer,
            mount=mount,
            panel_width_mm=panel_width_mm,
            panel_height_mm=panel_height_mm,
        )

    def _write_enclosure(
        self,
        sheet: Sheet,
        *,
        width_mm: float | None = None,
        height_mm: float | None = None,
        depth_mm: float | None = None,
        x: float | None = None,
        y: float | None = None,
        name: str | None = None,
        nema_type: str | None = None,
        material: str | None = None,
        catalog: str | None = None,
        manufacturer: str | None = None,
        mount: str | None = None,
        panel_width_mm: float | None = None,
        panel_height_mm: float | None = None,
    ) -> Enclosure:
        enc = sheet.enclosure or Enclosure()
        if width_mm is not None:
            enc.width_mm = float(width_mm)
        if height_mm is not None:
            enc.height_mm = float(height_mm)
        if depth_mm is not None:
            enc.depth_mm = float(depth_mm)
        if x is not None:
            enc.x = float(x)
        if y is not None:
            enc.y = float(y)
        if name is not None:
            enc.name = name
        if nema_type is not None:
            enc.nema_type = str(nema_type)
        if material is not None:
            enc.material = str(material)
        if catalog is not None:
            enc.catalog = str(catalog)
        if manufacturer is not None:
            enc.manufacturer = str(manufacturer)
        if mount is not None:
            enc.mount = str(mount)
        if panel_width_mm is not None:
            enc.panel_width_mm = float(panel_width_mm)
        if panel_height_mm is not None:
            enc.panel_height_mm = float(panel_height_mm)
        if width_mm is not None and height_mm is not None:
            enc.sized = True
        if str(catalog or "").strip() or str(manufacturer or "").strip():
            enc.sized = True
        if (
            panel_width_mm is not None
            and panel_height_mm is not None
            and float(panel_width_mm) > 1.0
            and float(panel_height_mm) > 1.0
        ):
            enc.sized = True
        sheet.enclosure = enc
        return enc

    def snap_layout_positions(
        self,
        sheet_id: str | None = None,
        *,
        refs: set[str] | None = None,
    ) -> None:
        """Seat DIN devices on the nearest rail.

        ``refs`` limits the snap to those devices. An empty set seats nothing.
        Omit it only when the whole sheet should be seated on purpose.
        """

        sheet = self.sheet(sheet_id)
        if not is_layout_sheet(sheet) or not sheet.rails:
            return
        if refs is not None and not refs:
            return
        for comp in sheet.components:
            if refs is not None and comp.id not in refs:
                continue
            device = self.catalog.get(comp.library_id)
            if not effective_footprint(device, comp).din_rail:
                continue
            rail = nearest_rail(sheet, comp.y + clip_offset(device, comp))
            if rail is not None:
                comp.x, comp.y = place_on_rail(device, rail, comp.x, component=comp)

    def layout_from_schematic(self, sheet_id: str | None = None) -> dict[str, Any]:
        """Copy unique schematic devices onto a panel_layout sheet and pack them on DIN rails.

        The enclosure already on the sheet is kept. This does not invent a
        cabinet size. With no enclosure, the result is needs-info and nothing
        is placed.
        """
        dest = self.sheet(sheet_id)
        if dest.kind != "panel_layout":
            raise ValueError(f"sheet '{dest.id}' is {dest.kind}, need panel_layout")

        seen: set[str] = set()
        din: list[tuple[str, Any]] = []
        field: list[tuple[str, Any]] = []
        unplaced: list[dict[str, str]] = []
        for src in self.project.sheets:
            if src.kind not in ("schematic", "io"):
                continue
            for comp in src.components:
                if comp.id in seen:
                    continue
                seen.add(comp.id)
                device = self.catalog.get(comp.library_id)
                fp = effective_footprint(device, comp)
                if is_child_contact(device, comp) or not is_incabinet(device, comp):
                    continue
                stub = str((comp.properties or {}).get("reconstruct_schematic_stub") or "").lower()
                need = str((comp.properties or {}).get("reconstruct_needs_info") or "").lower()
                if stub in {"1", "true", "yes"} or need in {"1", "true", "yes"}:
                    continue
                if str(comp.library_id or "") == "reconstruct.needs_info":
                    continue
                if occupies_panel_layout(device, comp) and not is_layout_ready(
                    device, comp, parts=self.parts
                ):
                    unplaced.append(
                        {
                            "ref": comp.id,
                            "library_id": comp.library_id,
                            "reason": layout_ready_error(comp.id, device, comp, parts=self.parts),
                        }
                    )
                    continue
                w, h = footprint_size(device, comp)
                if w <= 0 or h <= 0:
                    continue
                item = (comp.id, device, dict(comp.properties), comp.library_id)
                if fp.din_rail:
                    din.append(item)
                else:
                    field.append(item)

        from panelcad.core.layout import enclosure_is_sized

        if not enclosure_is_sized(dest.enclosure, dest.page):
            return {
                "sheet": dest.id,
                "needs_info": True,
                "reason": (
                    "No enclosure is set. Size the enclosure before building the layout. "
                    "A generic box is not used."
                ),
                "rails": len(dest.rails),
                "trays": len(dest.trays),
                "din": 0,
                "field": 0,
                "skipped": len(seen),
                "unplaced": unplaced,
                "cabinet": None,
                "pictures": 0,
            }

        if not din and not field:
            return {
                "sheet": dest.id,
                "rails": len(dest.rails),
                "trays": len(dest.trays),
                "din": 0,
                "field": 0,
                "skipped": len(seen),
                "unplaced": unplaced,
                "cabinet": None,
                "pictures": 0,
            }

        def _strip_hardware(device: Any, props: dict[str, Any]) -> bool:
            category = str(getattr(device, "category", "") or "").lower()
            if category.startswith("connection.terminal") or category.startswith("connection.accessory"):
                return True
            accessory = str((props or {}).get("accessory") or "")
            return accessory in {"end_stop", "end_plate", "partition", "terminal"}

        # Terminals and end stops pack after the other DIN modules so a stop
        # is not dropped onto a fuse that already occupies the rail.
        din_body = [item for item in din if not _strip_hardware(item[1], item[2])]
        din_strip = [item for item in din if _strip_hardware(item[1], item[2])]
        din = din_body + din_strip

        ghosts_din = [
            Component(id=ref, library_id=lib, x=0.0, y=0.0, scale=1.0, properties=props)
            for ref, _d, props, lib in din
        ]
        ghosts_field = [
            Component(id=ref, library_id=lib, x=0.0, y=0.0, scale=1.0, properties=props)
            for ref, _d, props, lib in field
        ]
        kept = dest.enclosure
        wx, wy, ww, wh = working_rect(dest)
        avail_w = max(20.0, ww - 8.0)
        avail_h = max(20.0, wh - 12.0)
        page_scale = float(kept.draw_scale or 0.0) or 1.0
        drawn_w = float(kept.width_mm) * page_scale
        drawn_h = float(kept.height_mm) * page_scale
        if drawn_w > avail_w + 1.0 or drawn_h > avail_h + 1.0:
            page_scale = fit_standard_enclosure_scale(
                float(kept.width_mm), float(kept.height_mm), avail_w, avail_h
            )
            kept.draw_scale = page_scale
            drawn_w = float(kept.width_mm) * page_scale
            drawn_h = float(kept.height_mm) * page_scale
            kept.x, kept.y = center_in_frame((wx, wy, ww, wh), drawn_w, drawn_h)
        ox, oy = float(kept.x), float(kept.y)
        rec = {
            "width_mm": float(kept.width_mm),
            "height_mm": float(kept.height_mm),
            "depth_mm": float(kept.depth_mm),
            "name": kept.name,
            "catalog": kept.catalog,
            "manufacturer": kept.manufacturer,
            "nema": kept.nema_type,
        }
        true_enc = Enclosure(
            x=0.0,
            y=0.0,
            width_mm=float(kept.width_mm),
            height_mm=float(kept.height_mm),
            depth_mm=float(kept.depth_mm),
        )
        rails_t, trays_t = panel_furniture(true_enc)
        placed_din, rails_t = pack_din(
            [(ref, d, g) for (ref, d, _p, _lib), g in zip(din, ghosts_din)],
            rails_t,
            trays=trays_t,
        )
        blocked: list[tuple[float, float, float, float]] = []
        by_ref = {ref: (props, lib, dev) for ref, dev, props, lib in din}
        for ref, x, y in placed_din:
            props, lib, dev = by_ref[ref]
            ghost = next(g for g in ghosts_din if g.id == ref)
            w, h = footprint_size(dev, ghost)
            blocked.append((x, y, w, h))
        for tray in trays_t:
            blocked.append(tray_aabb(tray))
        for rail in rails_t:
            blocked.append(rail_aabb(rail))
        inner = inner_backplate(true_enc, trays_t)
        by_field = {ref: (props, lib, d) for ref, d, props, lib in field}
        field_places = pack_field(
            [(ref, d, g) for (ref, d, _p, _lib), g in zip(field, ghosts_field)],
            true_enc,
            blocked=blocked,
            bounds=inner,
        )
        ox, oy, sc = dest.enclosure.x, dest.enclosure.y, page_scale
        dest.rails = [
            Rail(
                id=r.id,
                x=ox + r.x * sc,
                y=oy + r.y * sc,
                length_mm=r.length_mm * sc,
                height_mm=rail_height(r) * sc,
                type=r.type,
            )
            for r in rails_t
        ]
        dest.trays = [
            WireTray(
                id=t.id,
                x=ox + t.x * sc,
                y=oy + t.y * sc,
                length_mm=t.length_mm * sc,
                width_mm=t.width_mm * sc,
                orientation=t.orientation,
            )
            for t in trays_t
        ]
        fit_rails_between_ducts(dest.rails, dest.trays, dest.enclosure)
        from panelcad.reconstruct.placeholder import is_needs_info_component

        kept_placeholders = [comp for comp in dest.components if is_needs_info_component(comp)]
        dest.components = []
        for ref, x, y in placed_din:
            props, lib, _dev = by_ref[ref]
            dest.components.append(
                Component(id=ref, library_id=lib, x=ox + x * sc, y=oy + y * sc, scale=sc, properties=props)
            )
        for ref, x, y, scale in field_places:
            props, lib, _d = by_field[ref]
            dest.components.append(
                Component(
                    id=ref,
                    library_id=lib,
                    x=ox + x * sc,
                    y=oy + y * sc,
                    scale=sc * scale,
                    properties=props,
                )
            )
        dest.components.extend(kept_placeholders)
        from panelcad.core.layout import align_row_to_rail

        def _device_of(comp: Component):
            try:
                return self.catalog.get(comp.library_id)
            except KeyError:
                return None

        for rail in dest.rails:
            align_row_to_rail(rail, dest.components, _device_of)
        left, top, right, bottom = backplate_sheet_bounds(dest.enclosure)
        crowded = nudge_components_apart(
            dest.components,
            _device_of,
            bounds=(left, top, right, bottom),
        )
        if crowded:
            from panelcad.reconstruct.placeholder import is_needs_info_component

            drop: set[str] = set()
            for ref in crowded:
                comp = next((item for item in dest.components if item.id == ref), None)
                if comp is not None and is_needs_info_component(comp):
                    if not any(row.get("ref") == ref for row in unplaced):
                        unplaced.append(
                            {
                                "ref": ref,
                                "library_id": comp.library_id,
                                "reason": (
                                    f"{ref} does not fit in the enclosure without overlapping another part. "
                                    "Left in place because it still needs info."
                                ),
                            }
                        )
                    continue
                drop.add(ref)
                if any(row.get("ref") == ref for row in unplaced):
                    continue
                library_id = comp.library_id if comp is not None else ""
                unplaced.append(
                    {
                        "ref": ref,
                        "library_id": library_id,
                        "reason": f"{ref} does not fit in the enclosure without overlapping another part",
                    }
                )
            if drop:
                dest.components = [comp for comp in dest.components if comp.id not in drop]
        plate = backplate_sheet_bounds(dest.enclosure)
        dest.rails = [rail for rail in dest.rails if body_inside(*rail_aabb(rail), plate)]
        overflow: list[Component] = []
        kept_on_plate: list[Component] = []
        for comp in dest.components:
            if is_needs_info_component(comp):
                kept_on_plate.append(comp)
                continue
            try:
                device = self.catalog.get(comp.library_id)
            except KeyError:
                kept_on_plate.append(comp)
                continue
            width, height = footprint_size(device, comp)
            outside = width > 0 and height > 0 and not body_inside(comp.x, comp.y, width, height, plate)
            on_duct = width > 0 and height > 0 and footprint_overlaps_tray(
                comp.x, comp.y, width, height, dest.trays
            )
            if outside or on_duct:
                overflow.append(comp)
                continue
            kept_on_plate.append(comp)
        dest.components = kept_on_plate
        from panelcad.core.layout import rails_holding_parts, trim_rails_out_of_side_ducts

        dest.rails = rails_holding_parts(dest.rails, dest.components, _device_of)
        trim_rails_out_of_side_ducts(dest.rails, dest.trays)
        on_rail: list[Component] = []
        for comp in dest.components:
            if is_needs_info_component(comp):
                on_rail.append(comp)
                continue
            try:
                device = self.catalog.get(comp.library_id)
            except KeyError:
                on_rail.append(comp)
                continue
            if sits_fully_on_a_rail(comp, device, dest.rails):
                on_rail.append(comp)
                continue
            overflow.append(comp)
        dest.components = on_rail
        dest.rails = rails_holding_parts(dest.rails, dest.components, _device_of)
        box = dest.enclosure
        size = f"{format_mm(box.width_mm)} × {format_mm(box.height_mm)}"
        for comp in overflow:
            if any(row.get("ref") == comp.id for row in unplaced):
                continue
            unplaced.append(
                {
                    "ref": comp.id,
                    "library_id": comp.library_id,
                    "reason": (
                        f"{comp.id} does not fit in the {size} mm enclosure. "
                        "Size a larger enclosure."
                    ),
                }
            )
        noted = {str(row.get("ref") or "") for row in unplaced}
        on_sheet = {comp.id for comp in dest.components}
        for ref, _dev, _props, lib in [*din, *field]:
            if ref in on_sheet or ref in noted:
                continue
            unplaced.append(
                {
                    "ref": ref,
                    "library_id": lib,
                    "reason": (
                        f"{ref} does not fit in the {size} mm enclosure. "
                        "Size a larger enclosure."
                    ),
                }
            )
            noted.add(ref)
        fit_reasons = {
            str(row.get("ref") or ""): str(row.get("reason") or "")
            for row in unplaced
            if "does not fit" in str(row.get("reason") or "")
        }
        if fit_reasons:
            self.project.properties["layout_unplaced"] = {
                "sheet": dest.id,
                "width_mm": float(box.width_mm),
                "height_mm": float(box.height_mm),
                "depth_mm": float(box.depth_mm),
                "reasons": fit_reasons,
            }
        else:
            self.project.properties.pop("layout_unplaced", None)
        placed_ids = {comp.id for comp in dest.components}
        din_n = sum(1 for ref, _x, _y in placed_din if ref in placed_ids)
        field_n = sum(1 for ref, _x, _y, _scale in field_places if ref in placed_ids)
        pictures = self.apply_cut_sheet_pictures()

        return {
            "sheet": dest.id,
            "needs_info": False,
            "rails": len(dest.rails),
            "trays": len(dest.trays),
            "din": din_n,
            "field": field_n,
            "skipped": len(seen) - din_n - field_n,
            "unplaced": unplaced,
            "cabinet": rec,
            "pictures": pictures.get("applied", 0),
        }

    def recommend_cabinet(self, sheet_id: str | None = None, apply: bool = False) -> dict[str, Any]:
        """Size a cabinet from BOM rows with incabinet=yes. apply=True writes the enclosure."""
        from panelcad.core.layout import fit_enclosure_scale, recommend_cabinet_size
        from panelcad.library.panel import is_incabinet
        from panelcad.render.titleblock import working_rect

        seen: set[str] = set()
        din: list[tuple[Any, Any]] = []
        field: list[tuple[Any, Any]] = []
        refs: list[str] = []
        for src in self.project.sheets:
            if src.kind not in ("schematic", "io"):
                continue
            for comp in src.components:
                if comp.id in seen:
                    continue
                seen.add(comp.id)
                device = self.catalog.get(comp.library_id)
                if is_child_contact(device, comp) or not is_incabinet(device, comp):
                    continue
                from panelcad.library.panel import is_info_needed_placeholder, should_skip_layout_occupant

                if is_info_needed_placeholder(comp):
                    continue
                if should_skip_layout_occupant(self.project, device, comp, sheet=str(sheet_id or "")):
                    continue
                refs.append(comp.id)
                ghost = Component(
                    id=comp.id,
                    library_id=comp.library_id,
                    x=0.0,
                    y=0.0,
                    scale=1.0,
                    properties=dict(comp.properties),
                )
                fp = effective_footprint(device, ghost)
                item = (device, ghost)
                if fp.din_rail:
                    din.append(item)
                else:
                    field.append(item)
        rec = recommend_cabinet_size(din, field)
        rec["incabinet_count"] = len(refs)
        rec["incabinet_refs"] = refs
        rec["name"] = (
            f"Enclosure {rec['width_mm']:.0f} × {rec['height_mm']:.0f} × {rec['depth_mm']:.0f} mm, "
            f"{len(refs)} BOM items in cabinet"
        )
        dest = None
        if sheet_id:
            dest = self.sheet(sheet_id)
        else:
            dest = next((sh for sh in self.project.sheets if sh.kind == "panel_layout"), None)
        if apply and dest is not None:
            wx, wy, ww, wh = working_rect(dest)
            scale = fit_enclosure_scale(float(rec["width_mm"]), float(rec["height_mm"]), ww - 8.0, wh - 12.0)
            self.set_enclosure(
                width_mm=float(rec["width_mm"]),
                height_mm=float(rec["height_mm"]),
                depth_mm=float(rec["depth_mm"]),
                x=wx + 4.0,
                y=wy + 4.0,
                name=str(rec["name"]),
                sheet_id=dest.id,
            )
            dest.enclosure.draw_scale = scale
            dest.rails = []
            rec["applied"] = True
            rec["sheet"] = dest.id
            rec["draw_scale"] = round(scale, 4)
        else:
            rec["applied"] = False
        return rec

    def compute_heat_load(self, **overrides: Any) -> dict[str, Any]:
        """Watt loss, rise, and cooling for the enclosure already chosen. Does not resize it."""

        from panelcad.core.thermal import compute_heat_load
        from panelcad.render.thermal_page import write_thermal_pdf

        path = overrides.pop("path", None)
        report = compute_heat_load(self.project, self.catalog, self.parts, **overrides)
        if not path:
            return report
        _ensure_qt_app()
        return write_thermal_pdf(self, path, report=report)

    @_traced_path("export_thermal_pdf")
    def export_thermal_pdf(self, path: Path | str, **overrides: Any) -> dict[str, Any]:
        """Write the thermal report PDF. Overrides apply to this run only."""

        return self.compute_heat_load(path=path, **overrides)

    def set_ambient(self, **fields: Any) -> dict[str, Any]:
        """Store thermal settings on the project. Does not change the enclosure. One undo."""

        from panelcad.core.thermal import THERMAL_KEY, settings_from

        allowed = {
            "ambient_c",
            "max_internal_c",
            "min_internal_c",
            "min_ambient_c",
            "component_max_c",
            "mount",
            "material",
            "nema",
            "outdoor",
            "altitude_m",
            "vfd_efficiency",
            "psu_efficiency",
            "transformer_efficiency",
            "estimates",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unknown thermal setting '{unknown[0]}'")
        optional = {"min_internal_c", "min_ambient_c"}
        floats = allowed - {"mount", "material", "nema", "outdoor", "estimates"} - optional

        def _run() -> dict[str, Any]:
            raw = self.project.properties.get(THERMAL_KEY)
            data = dict(raw) if isinstance(raw, dict) else {}
            for key, value in fields.items():
                if key == "estimates":
                    if not isinstance(value, dict):
                        raise ValueError("estimates must be a map of ref to watts")
                    current = dict(data.get("estimates") or {})
                    for ref, watts in value.items():
                        if watts is None:
                            current.pop(str(ref), None)
                            continue
                        number = float(watts)
                        if number < 0:
                            raise ValueError("watt estimate cannot be negative")
                        current[str(ref)] = number
                    data["estimates"] = current
                    continue
                if key in optional and value is None:
                    data[key] = None
                    continue
                if key in {"mount", "material", "nema"}:
                    data[key] = "" if value is None else str(value)
                    continue
                if key == "outdoor":
                    data["outdoor"] = bool(value)
                    continue
                if value is None or key not in floats:
                    continue
                number = float(value)
                if key == "altitude_m" and number < 0:
                    raise ValueError("altitude cannot be negative")
                if key.endswith("efficiency") and not 0.5 <= number <= 0.99:
                    raise ValueError(f"{key} must be between 0.5 and 0.99")
                data[key] = number
            self.project.properties[THERMAL_KEY] = data
            return settings_from(self.project).to_dict()

        return self._undoable("set_ambient", "thermal settings", _run)

    def set_watt_estimate(self, ref: str, watts: float | None) -> dict[str, Any]:
        """User watt estimate on every sheet with this tag. Labeled ESTIMATE. One undo."""

        def _run() -> dict[str, Any]:
            found = 0
            stored: float | None
            if watts is None:
                stored = None
            else:
                stored = float(watts)
                if stored < 0:
                    raise ValueError("watt estimate cannot be negative")
            for sheet in self.project.sheets:
                for comp in sheet.components:
                    if comp.id != str(ref):
                        continue
                    if stored is None:
                        comp.properties.pop("watt_estimate", None)
                    else:
                        comp.properties["watt_estimate"] = stored
                    found += 1
            if found == 0:
                raise KeyError(f"no component '{ref}'")
            return {"ref": str(ref), "watt_estimate": stored, "sheets": found}

        return self._undoable("set_watt_estimate", f"watt estimate {ref}", _run)

    def recommend_enclosure(self, sheet_id: str | None = None, **options: Any) -> dict[str, Any]:
        """Shop-style enclosure size from footprints, duct, spare space, and heat. Never writes."""

        from panelcad.core.enclosure import options_from_defaults, recommend_enclosure
        from panelcad.library.enclosures import load_enclosures

        project_dir = self.path.parent if self.path else None
        library = load_enclosures(project_dir)
        stored = self.project.properties.get("enclosure_sizing") or {}
        merged = dict(library.defaults)
        if isinstance(stored, dict):
            merged.update(stored)
        merged.update({k: v for k, v in options.items() if v is not None})
        rec = recommend_enclosure(
            self.project,
            self.catalog,
            library=library,
            options=options_from_defaults(merged),
            project_dir=project_dir,
        )
        rec["applied"] = False
        if sheet_id:
            rec["sheet"] = sheet_id
        return rec

    def apply_enclosure(
        self,
        sheet_id: str | None = None,
        place_parts: bool = False,
        recommendation: dict[str, Any] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Write the recommended box (and optionally the arrangement) as one undo step."""

        rec = dict(recommendation or self.recommend_enclosure(sheet_id=sheet_id, **options))

        def _run() -> dict[str, Any]:
            return self._write_enclosure_recommendation(rec, sheet_id=sheet_id, place_parts=place_parts)

        name = ((rec.get("enclosure") or {}).get("name") or "enclosure")
        return self._undoable("apply_enclosure", f"size enclosure {name}", _run)

    def _write_enclosure_recommendation(
        self,
        rec: dict[str, Any],
        *,
        sheet_id: str | None,
        place_parts: bool,
        keep_size: bool = False,
    ) -> dict[str, Any]:
        from panelcad.core.enclosure import classify_role, furniture_from_arrangement
        from panelcad.core.layout import (
            center_in_frame,
            finish_rails,
            fit_rails_between_ducts,
            fit_standard_enclosure_scale,
            reseat_din_modules,
            rescale_parts_onto_enclosure,
        )
        from panelcad.library.panel import display_footprint_size
        from panelcad.render.titleblock import working_rect

        box = rec.get("enclosure") or {}
        if not box:
            raise ValueError("no enclosure recommendation to apply")
        dest = None
        if sheet_id:
            dest = self.sheet(sheet_id)
        else:
            dest = next((sh for sh in self.project.sheets if sh.kind == "panel_layout"), None)
        if dest is None:
            dest = self.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
        if dest.kind != "panel_layout":
            raise ValueError(f"sheet '{dest.id}' is {dest.kind}, need panel_layout")
        old_enc = dest.enclosure.model_copy(deep=True) if dest.enclosure is not None else None
        snapshot = [comp.model_copy(deep=True) for comp in dest.components]

        def drawn_size(comp: Component) -> tuple[float, float]:
            device = self.catalog.devices.get(comp.library_id)
            if device is None:
                return (0.0, 0.0)
            return display_footprint_size(device, comp)

        def is_door(comp: Component) -> bool:
            device = self.catalog.devices.get(comp.library_id)
            if device is None:
                return False
            return classify_role(device, comp) == "door"

        wx, wy, ww, wh = working_rect(dest)
        kept = dest.enclosure if keep_size and dest.enclosure is not None else None
        if kept is not None and kept.width_mm > 1.0 and kept.height_mm > 1.0:
            width = float(kept.width_mm)
            height = float(kept.height_mm)
            depth = float(kept.depth_mm)
        else:
            width = float(box["width_mm"])
            height = float(box["height_mm"])
            depth = float(box["depth_mm"])
        scale = fit_standard_enclosure_scale(width, height, ww, wh)
        ox, oy = center_in_frame((wx, wy, ww, wh), width * scale, height * scale)
        from panelcad.core.panels import find_panel

        owned = find_panel(self.project, dest.panel) if dest.kind == "panel_layout" else None
        box_id = str(box.get("id") or "").strip()
        if keep_size and kept is not None:
            kept.x = ox
            kept.y = oy
            kept.draw_scale = scale
            dest.enclosure = kept
        elif owned is not None:
            if not box_id:
                raise ValueError(
                    f"{dest.id} belongs to a panel. Size Enclosure did not name a library enclosure."
                )
            self._apply_panel_enclosure(owned, box_id, replace=True)
            dest.enclosure.x = ox
            dest.enclosure.y = oy
        else:
            self.set_enclosure(
                width_mm=width,
                height_mm=height,
                depth_mm=depth,
                x=ox,
                y=oy,
                name=str(box.get("name") or ""),
                sheet_id=dest.id,
                nema_type=str(box.get("nema") or ""),
                material=str(box.get("material") or ""),
                catalog=str(box.get("catalog") or ""),
                manufacturer=str(box.get("manufacturer") or ""),
                mount=str(box.get("mount") or ""),
                panel_width_mm=float(box.get("panel_width_mm") or 0.0),
                panel_height_mm=float(box.get("panel_height_mm") or 0.0),
            )
        dest.enclosure.draw_scale = scale
        items = list((rec.get("arrangement") or {}).get("items") or [])
        if keep_size and kept is not None:
            from panelcad.core.layout import spread_arrangement_onto_plate

            plate_w = float(kept.panel_width_mm or 0.0) or width
            plate_h = float(kept.panel_height_mm or 0.0) or height
            inset_x = max(0.0, (width - plate_w) / 2.0)
            inset_y = max(0.0, (height - plate_h) / 2.0)
            items = spread_arrangement_onto_plate(
                items,
                (inset_x, inset_y, inset_x + plate_w, inset_y + plate_h),
            )
        rails, trays = furniture_from_arrangement(items, ox, oy, scale)
        dest.rails = rails
        dest.trays = trays
        warnings: list[str] = []
        by_id = {comp.id: comp for comp in snapshot}
        if place_parts:
            placed: list[Component] = []
            seen: set[str] = set()
            for item in items:
                if item.get("kind") not in {"device", "block"}:
                    continue
                ref = str(item.get("ref") or "")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                src = by_id.get(ref)
                if src is None:
                    for sheet in self.project.sheets:
                        try:
                            src = sheet.component(ref)
                            break
                        except KeyError:
                            continue
                if src is None or is_door(src):
                    continue
                from panelcad.library.panel import should_skip_layout_occupant

                device = self.catalog.devices.get(src.library_id)
                if should_skip_layout_occupant(self.project, device, src, sheet=dest.id):
                    continue
                placed.append(
                    Component(
                        id=ref,
                        library_id=src.library_id,
                        x=ox + float(item["x"]) * scale,
                        y=oy + float(item["y"]) * scale,
                        scale=scale,
                        properties=dict(src.properties),
                    )
                )
            dest.components = placed
        else:
            warnings.extend(
                rescale_parts_onto_enclosure(
                    dest.components,
                    old=old_enc,
                    new=dest.enclosure,
                    trays=dest.trays,
                    size_of=drawn_size,
                )
            )
            fit_rails_between_ducts(dest.rails, dest.trays, dest.enclosure)

            def _device(comp: Component):
                try:
                    return self.catalog.get(comp.library_id)
                except KeyError:
                    return None

            warnings.extend(
                reseat_din_modules(
                    dest.components,
                    dest.rails,
                    dest.trays,
                    dest.enclosure,
                    device_of=_device,
                    order_x={comp.id: float(comp.x) for comp in snapshot},
                )
            )
        door = next((sh for sh in self.project.sheets if sh.kind == "door_layout"), None)
        pending_door = [comp for comp in snapshot if is_door(comp)]
        if door is None and place_parts and pending_door:
            from panelcad.core.circuit_builder import _ensure_kind_sheet

            door = self.sheet(_ensure_kind_sheet(self, "door_layout", "door", "Door"))
            warnings.append(
                "Created door sheet "
                + door.id
                + " for "
                + ", ".join(comp.id for comp in pending_door)
            )
        def _hand_placed(comp: Component) -> bool:
            props = comp.properties or {}
            if props.get("layout_band") or props.get("layout_circuit") or props.get("layout_furniture"):
                return False
            if props.get("addon") or str(props.get("accessory") or ""):
                return False
            return True

        hand_placed = [comp for comp in door.components if _hand_placed(comp)] if door is not None else []
        if hand_placed:
            # A fitted box would leave these parts in the old sheet frame, then
            # cutout math would divide them by the new scale. Leave the door.
            warnings.append(
                "Door already has placed parts ("
                + ", ".join(comp.id for comp in hand_placed)
                + "); left that sheet unchanged."
            )
        elif door is not None:
            door_enc = door.enclosure or Enclosure()
            door_enc.width_mm = width
            door_enc.height_mm = height
            door_enc.depth_mm = min(depth, 80.0)
            if keep_size and kept is not None:
                door_enc.name = kept.name or door_enc.name
                door_enc.nema_type = kept.nema_type
                door_enc.catalog = kept.catalog
                door_enc.manufacturer = kept.manufacturer
                door_enc.material = kept.material
                door_enc.mount = kept.mount
            else:
                door_enc.name = str(box.get("name") or door_enc.name)
                door_enc.nema_type = str(box.get("nema") or "")
            door.enclosure = door_enc
            if place_parts:
                dwx, dwy, dww, dwh = working_rect(door)
                dscale = fit_standard_enclosure_scale(width, height, dww, dwh)
                door_enc.x, door_enc.y = center_in_frame((dwx, dwy, dww, dwh), width * dscale, height * dscale)
                door_enc.draw_scale = dscale
                existing_door = {comp.id: comp for comp in door.components}
                door_placed: list[Component] = []
                for item in (rec.get("door") or {}).get("items") or []:
                    ref = str(item.get("ref") or "")
                    src = existing_door.get(ref)
                    if src is None:
                        src = by_id.get(ref)
                    if src is None:
                        for sheet in self.project.sheets:
                            try:
                                src = sheet.component(ref)
                                break
                            except KeyError:
                                continue
                    if src is None:
                        continue
                    door_placed.append(
                        Component(
                            id=ref,
                            library_id=src.library_id,
                            x=door_enc.x + float(item["x"]) * dscale,
                            y=door_enc.y + float(item["y"]) * dscale,
                            scale=dscale,
                            properties=dict(src.properties),
                        )
                    )
                placed_ids = {comp.id for comp in door_placed}
                rescued = [
                    comp.model_copy(deep=True)
                    for comp in snapshot
                    if is_door(comp) and comp.id not in placed_ids
                ]
                if rescued:
                    blockers: list[tuple[float, float, float, float]] = []
                    for comp in door_placed:
                        part_w, part_h = drawn_size(comp)
                        if part_w > 0 and part_h > 0:
                            blockers.append((comp.x, comp.y, part_w, part_h))
                    warnings.extend(
                        rescale_parts_onto_enclosure(
                            rescued,
                            old=old_enc,
                            new=door_enc,
                            trays=door.trays,
                            size_of=drawn_size,
                            blockers=blockers,
                        )
                    )
                    warnings.append(
                        "Kept door-mounted parts on the door sheet: " + ", ".join(comp.id for comp in rescued)
                    )
                if door_placed:
                    door.components = door_placed
                if rescued:
                    have = {comp.id for comp in door.components}
                    door.components.extend(comp for comp in rescued if comp.id not in have)
        def _device_of(comp: Component):
            try:
                return self.catalog.get(comp.library_id)
            except KeyError:
                return None

        prefix = "iec."
        for comp in dest.components:
            if str(comp.library_id).startswith("nema."):
                prefix = "nema."
                break
        stop_device = None
        for library_id in (f"{prefix}accessories.end_stop", "iec.accessories.end_stop"):
            try:
                stop_device = self.catalog.get(library_id)
                break
            except KeyError:
                continue
        added_stops = finish_rails(
            dest.components,
            dest.rails,
            dest.trays,
            dest.enclosure,
            _device_of,
            stop_device,
        )
        if added_stops:
            have = {comp.id for comp in dest.components}
            dest.components.extend(comp for comp in added_stops if comp.id not in have)
            warnings.append("Added rail end stops: " + ", ".join(comp.id for comp in added_stops))
        self.project.properties["enclosure_sizing"] = dict(rec.get("options") or {})
        if warnings:
            rec["warnings"] = warnings
        rec["applied"] = True
        rec["sheet"] = dest.id
        rec["place_parts"] = bool(place_parts)
        rec["draw_scale"] = round(scale, 4)
        return rec

    def set_address(
        self,
        ref: str,
        *,
        ip: str | None = None,
        node: str | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        """Set IP and/or node/station on a device (shown on network pages)."""
        # Write onto every sheet that has this ref so schematic + comms stay in sync.
        updated = 0
        for sheet in self.project.sheets:
            if sheet_id and sheet.id != sheet_id:
                continue
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            if ip is not None:
                if str(ip).strip():
                    comp.properties["ip"] = str(ip).strip()
                    comp.properties["address"] = str(ip).strip()
                else:
                    comp.properties.pop("ip", None)
                    comp.properties.pop("address", None)
            if node is not None:
                if str(node).strip():
                    comp.properties["node"] = str(node).strip()
                    comp.properties["station"] = str(node).strip()
                else:
                    comp.properties.pop("node", None)
                    comp.properties.pop("station", None)
            updated += 1
        if updated == 0:
            raise KeyError(f"no component '{ref}'")
        sheet = self.sheet(sheet_id)
        try:
            comp = sheet.component(ref)
        except KeyError:
            for s in self.project.sheets:
                try:
                    comp = s.component(ref)
                    break
                except KeyError:
                    continue
        return {"id": ref, "ip": str(comp.properties.get("ip") or ""), "node": str(comp.properties.get("node") or ""), "address": network_address(comp)}

    def set_pin_labels(self, ref: str, labels: dict[str, str], sheet_id: str | None = None) -> Component:
        """OEM terminal text on this instance (X0, Y3, OUT0, Si4-Si5, …)."""
        comp = self.sheet(sheet_id).component(ref)
        current = dict(comp.properties.get("pin_labels") or {})
        current.update({str(k): str(v) for k, v in labels.items() if str(v).strip()})
        comp.properties["pin_labels"] = current
        return comp

    def set_component_properties(
        self, ref: str, properties: dict[str, Any], sheet_id: str | None = None
    ) -> Component:
        from panelcad.library.panel import NEEDS_INFO_KEY, needs_info_library_id

        comp = self.sheet(sheet_id).component(ref)
        incoming = strip_placeholder_guesses(
            comp.library_id,
            sanitize_caller_properties(
                {str(k): v for k, v in properties.items()}, parts=self.parts
            ),
        )
        refuse_nonfinite_sizes(incoming)
        if str(incoming.get(NEEDS_INFO_KEY) or "").strip().lower() in {"1", "true", "yes", "y"}:
            if not needs_info_library_id(str(comp.library_id or "")):
                raise LayoutNotReadyError(
                    "reconstruct_needs_info is only valid on reconstruct.needs_info — "
                    "it cannot mark a library symbol"
                )
        identity = {"catalog", "part_number", "manufacturer"}
        targets = [comp]
        if any(key in incoming for key in identity):
            targets = []
            for sheet in self.project.sheets:
                try:
                    targets.append(sheet.component(ref))
                except KeyError:
                    continue
            if not targets:
                targets = [comp]
        from panelcad.reconstruct.layout import drop_envelope_confirmation

        for target in targets:
            if envelope_edit_changes(target.properties, incoming):
                drop_envelope_confirmation(
                    target,
                    was=(
                        target.properties.get("width_mm"),
                        target.properties.get("height_mm"),
                        target.properties.get("depth_mm"),
                    ),
                )
            merged = {**target.properties, **incoming}
            if str(incoming.get("dim_source") or "") == "catalog" and not assigned_catalog_ready_props(
                merged, parts=self.parts
            ):
                incoming.pop("dim_source", None)
            target.properties.update(incoming)
        if any(key in incoming for key in identity):
            self.drop_stale_assignment(ref)
        if isinstance(comp.properties.get("io_point"), dict):
            from panelcad.core.io_sheets import pull_io_sheet_edits

            pull_io_sheet_edits(self.project, self.catalog)
        return comp

    def update_check(self, feed: str | None = None) -> dict[str, Any]:
        """Read the release feed. Does not download or install."""

        from panelcad.update.service import check_for_update
        from panelcad.update.settings import config_dir

        return check_for_update(feed=feed, config_dir=config_dir())

    def update_install(
        self,
        *,
        confirm: bool = False,
        feed: str | None = None,
        dry_run: bool = False,
        expected_version: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Verify the feed's installer and start the upgrade. Requires confirm=True.

        ``expected_version`` and ``expected_sha256`` pin the manifest the operator
        already saw. The install is refused when the feed has changed.
        """

        from panelcad.update.service import install_update
        from panelcad.update.settings import config_dir

        project = str(self.path) if self.path else None
        return install_update(
            confirm=confirm,
            feed=feed,
            dry_run=dry_run,
            relaunch_project=project,
            config_dir=config_dir(),
            expected_version=expected_version,
            expected_sha256=expected_sha256,
        )

    def update_rollback(self, *, confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Restore program files from the saved backup. Requires confirm=True."""

        from panelcad.update.service import rollback_update
        from panelcad.update.settings import config_dir

        project = str(self.path) if self.path else None
        return rollback_update(
            confirm=confirm,
            dry_run=dry_run,
            relaunch_project=project,
            config_dir=config_dir(),
        )

    def reconstruct_add_photos(
        self,
        paths: list[Path | str] | Path | str,
        role: str = "other",
        note: str = "",
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import add_photos

        items = paths if isinstance(paths, (list, tuple)) else [paths]
        return add_photos(self, list(items), role=role, note=note)

    def reconstruct_tag_photo(self, photo_id: str, role: str, note: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import tag_photo

        return tag_photo(self, photo_id, role, note)

    def reconstruct_list_photos(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import status

        return {"photos": status(self)["photos"]}

    def reconstruct_set_corners(self, photo_id: str, corners: list[Any]) -> dict[str, Any]:
        from panelcad.reconstruct.service import set_corners

        return set_corners(self, photo_id, corners)

    def reconstruct_set_scale(
        self,
        photo_id: str,
        kind: str,
        p1: Any,
        p2: Any,
        *,
        known_mm: float | None = None,
        catalog: str = "",
        manufacturer: str = "",
        dimension: str = "width",
        note: str = "",
        known_height_mm: float | None = None,
        face_width_mm: float | None = None,
        face_height_mm: float | None = None,
        backpanel: bool = False,
        slot: int | str | None = None,
        replace: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import set_scale

        return set_scale(
            self,
            photo_id,
            kind,
            p1,
            p2,
            known_mm=known_mm,
            catalog=catalog,
            manufacturer=manufacturer,
            dimension=dimension,
            note=note,
            known_height_mm=known_height_mm,
            face_width_mm=face_width_mm,
            face_height_mm=face_height_mm,
            backpanel=backpanel,
            slot=slot,
            replace=replace,
        )

    def reconstruct_clear_scale(self, photo_id: str) -> dict[str, Any]:
        from panelcad.reconstruct.service import clear_scale

        return clear_scale(self, photo_id)

    def reconstruct_identify(
        self,
        photo_id: str | None = None,
        *,
        vision: Any = None,
        recordings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import identify

        return identify(self, photo_id, vision=vision, recordings=recordings)

    def reconstruct_confirm_item(
        self,
        item_id: str,
        *,
        catalog: str = "",
        manufacturer: str = "",
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        tag: str = "",
        library_id: str = "",
        mount: str = "",
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import confirm_item

        return confirm_item(
            self,
            item_id,
            catalog=catalog,
            manufacturer=manufacturer,
            envelope_w_mm=envelope_w_mm,
            envelope_h_mm=envelope_h_mm,
            envelope_d_mm=envelope_d_mm,
            tag=tag,
            library_id=library_id,
            mount=mount,
        )

    def reconstruct_place_needs_info(self, photo_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import place_needs_info

        return place_needs_info(self, photo_id)

    def reconstruct_place_confirmed(self, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import place_confirmed

        return place_confirmed(self, sheet_id)

    def reconstruct_checklist(self) -> dict[str, Any]:
        from panelcad.reconstruct.checklist import rebuild_checklist
        from panelcad.reconstruct.models import ensure_reconstruction
        from panelcad.reconstruct.service import status

        rebuild_checklist(ensure_reconstruction(self.project))
        return {"checklist": status(self)["checklist"]}

    def reconstruct_next_question(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import next_question

        return next_question(self)

    def reconstruct_answer(
        self,
        step_id: str,
        text: str = "",
        *,
        photo: Path | str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import answer

        return answer(self, step_id, text, photo=photo, confirm=confirm)

    def reconstruct_confirm_connection(
        self,
        step_id: str | None = None,
        wire_id: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import confirm_connection

        return confirm_connection(self, step_id=step_id, wire_id=wire_id)

    def reconstruct_attach_print(self, path: Path | str, note: str = "") -> dict[str, Any]:
        from panelcad.reconstruct.service import attach_print

        return attach_print(self, path, note=note)

    def reconstruct_compare_print(self, path: Path | str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import compare_print

        return compare_print(self, path)

    def reconstruct_photo_region(
        self,
        item_id: str | None = None,
        ref: str | None = None,
        photo_id: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import photo_region

        return photo_region(self, item_id=item_id, ref=ref, photo_id=photo_id)

    def reconstruct_unconfirmed(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import list_unconfirmed

        return list_unconfirmed(self)

    def reconstruct_status(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import status

        return status(self)

    def describe(self, sheet_id: str | None = None) -> dict[str, Any]:
        """Full structured snapshot for an AI to read without the GUI."""
        target = [self.sheet(sheet_id)] if sheet_id else list(self.project.sheets)
        total = len(self.project.sheets)
        from panelcad.reconstruct.service import status as reconstruct_status
        from panelcad.core.spacing import page_fullness

        return {
            "name": self.project.name,
            "path": str(self.path) if self.path else None,
            "active_sheet": self.active_sheet_id,
            "panels": self.list_panels(),
            "active_panel": self.project.active_panel,
            "sheet_count": total,
            "title_block": self.get_title_block(),
            "wire_numbering": self.wire_numbering(),
            "cross_reference": self.cross_reference_settings(),
            "cross_reference_rows": self.cross_reference_rows(),
            "device_index": self.device_index(),
            "reconstruct": reconstruct_status(self) if self.project.reconstruct is not None else None,
            "sheets": [
                {
                    "index": self.project.sheets.index(s) + 1,
                    "count": total,
                    "id": s.id,
                    "title": s.title,
                    "kind": s.kind,
                    "panel": s.panel,
                    "closed": s.closed,
                    "description": s.description,
                    "components": self.list_components(s.id),
                    "wires": self.list_wires(s.id),
                    "rails": self.list_rails(s.id),
                    "density": page_fullness(s, self.catalog),
                    "enclosure": s.enclosure.model_dump() if s.enclosure else None,
                    "network": s.network.model_dump() if s.network else None,
                }
                for s in target
            ],
            "errors": self.validate(),
        }

    def validate(self) -> list[str]:
        from panelcad.core.checks import structural_messages

        return structural_messages(self.project, self.catalog)

    def _assert_pin(self, sheet: Sheet, pin_ref: str) -> None:
        from panelcad.core.checks import assert_pin_ref

        assert_pin_ref(self.project, self.catalog, sheet, pin_ref)

    def _ensure_instance_ports(self, ref: str, sheet_id: str | None = None) -> Component:
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        if not comp.ports:
            device = self.catalog.get(comp.library_id)
            comp.ports = [p.model_copy() for p in device.ports]
        return comp

    def list_ports(self, ref: str, sheet_id: str | None = None) -> list[dict[str, Any]]:
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        device = self.catalog.get(comp.library_id)
        return [p.model_dump() for p in effective_ports(device, comp)]

    def add_port(
        self,
        ref: str,
        port_id: str,
        kind: str = "serial",
        medium: str = "",
        protocol: str = "",
        connector: str = "",
        side: str | None = None,
        sheet_id: str | None = None,
    ) -> CommPort:
        comp = self._ensure_instance_ports(ref, sheet_id)
        pid = port_id.strip() or "COM"
        if any(p.id == pid for p in comp.ports):
            raise ValueError(f"{ref} already has port '{pid}'")
        port = parse_port(
            {
                "id": pid,
                "kind": kind,
                "medium": medium,
                "protocol": protocol,
                "connector": connector,
                "side": side or default_kind_side(kind),  # type: ignore[arg-type]
            }
        )
        comp.ports.append(port)
        return port

    def set_port(self, ref: str, port_id: str, sheet_id: str | None = None, **fields: Any) -> CommPort:
        comp = self._ensure_instance_ports(ref, sheet_id)
        for i, p in enumerate(comp.ports):
            if p.id != port_id:
                continue
            data = p.model_dump()
            data.update({k: v for k, v in fields.items() if v is not None})
            if "id" in fields and fields["id"]:
                data["id"] = str(fields["id"])
            comp.ports[i] = parse_port(data)
            return comp.ports[i]
        raise KeyError(f"{ref} has no port '{port_id}'")

    def delete_port(self, ref: str, port_id: str, sheet_id: str | None = None) -> None:
        comp = self._ensure_instance_ports(ref, sheet_id)
        before = len(comp.ports)
        comp.ports = [p for p in comp.ports if p.id != port_id]
        if len(comp.ports) == before:
            raise KeyError(f"{ref} has no port '{port_id}'")

    def set_network(
        self,
        name: str | None = None,
        medium: str | None = None,
        protocol: str | None = None,
        sheet_id: str | None = None,
    ) -> NetworkInfo:
        sheet = self.sheet(sheet_id)
        info = sheet.network or NetworkInfo()
        if name is not None:
            info.name = name
        if medium is not None:
            info.medium = medium  # type: ignore[assignment]
        if protocol is not None:
            info.protocol = protocol
        sheet.network = info
        if sheet.kind != "network":
            sheet.kind = "network"
        return info

    def layout_network(self, sheet_id: str | None = None, medium: str | None = None) -> dict[str, Any]:
        """Place devices that have matching comms ports onto a network sheet.

        A stored IP is copied onto the network node. A blank IP stays blank,
        and an explicit TBD stays TBD. This does not assign 192.168.x.x or
        any other address.
        """
        dest = self.sheet(sheet_id)
        if dest.kind != "network":
            dest.kind = "network"
        info = dest.network or NetworkInfo(name=dest.title or dest.id)
        filt = medium or (info.medium if info.medium != "mixed" else None)
        if medium:
            info.medium = medium  # type: ignore[assignment]
        dest.network = info
        dest.components = []
        dest.wires = []
        x, y = 36.0, 36.0
        placed = 0
        seen: set[str] = set()
        next_node = 1
        for src in self.project.sheets:
            if src.kind not in ("schematic", "io"):
                continue
            for comp in src.components:
                if comp.id in seen:
                    continue
                device = self.catalog.get(comp.library_id)
                ports = [p for p in effective_ports(device, comp) if port_matches_medium(p, filt)]
                if not ports:
                    continue
                seen.add(comp.id)
                props = dict(comp.properties)
                retain_known_ip(props)
                if filt == "serial" or any(p.kind == "serial" for p in ports):
                    if not str(props.get("node") or props.get("station") or "").strip():
                        props["node"] = str(next_node)
                        props["station"] = str(next_node)
                        next_node += 1
                for key in ("node", "station"):
                    if key in props:
                        comp.properties[key] = props[key]
                for key in ("ip", "address"):
                    if key in props and str(props.get(key) or "").strip():
                        comp.properties[key] = props[key]
                    else:
                        comp.properties.pop(key, None)
                dest.components.append(
                    Component(
                        id=comp.id,
                        library_id=comp.library_id,
                        x=x,
                        y=y,
                        properties=props,
                        ports=list(comp.ports),
                    )
                )
                placed += 1
                x += 90.0
                if x > 340.0:
                    x = 36.0
                    y += 70.0
        return {"sheet": dest.id, "medium": info.medium, "devices": placed}

    def layout_io(self, sheet_id: str | None = None) -> dict[str, Any]:
        """ANSI I/O: field in from the left, loads out to the right.

        Input cards default to the right, output cards to the left, combo in the middle.
        """
        from panelcad.core.cleanup import arrange_io
        from panelcad.render.io_ladder import is_io_module

        dest = self.sheet(sheet_id)
        dest.kind = "io"
        if not dest.components:
            seen: set[str] = set()
            for src in self.project.sheets:
                if src.id == dest.id or src.kind not in ("schematic", "io"):
                    continue
                for c in src.components:
                    if c.id in seen:
                        continue
                    device = self.catalog.get(c.library_id)
                    cat = device.category
                    if not (
                        is_io_module(device)
                        or cat.startswith("operator")
                        or "relay" in cat
                        or cat.startswith("drive")
                        or cat.startswith("load")
                    ):
                        continue
                    seen.add(c.id)
                    dest.components.append(
                        Component(
                            id=c.id,
                            library_id=c.library_id,
                            x=c.x,
                            y=c.y,
                            properties=dict(c.properties),
                            ports=list(c.ports),
                        )
                    )
        packed = arrange_io(dest, self.catalog)
        return {"sheet": dest.id, "modules": packed["modules"], "field": packed["field"]}


def _summary_place(self: Session, result: Any, args: tuple, kwargs: dict) -> str:
    return f"place {result.id} ({result.library_id}) at {result.x:g},{result.y:g}"


def _summary_wire(self: Session, result: Any, args: tuple, kwargs: dict) -> str:
    if isinstance(result, dict):
        return f"wire off-page {result.get('from')} → {result.get('to_sheet')}:{result.get('to')}"
    return f"wire {result.frm} → {result.to}" + (f"  [{result.net}]" if result.net else "")


def _summary_generic(op: str) -> Any:
    def fmt(self: Session, result: Any, args: tuple, kwargs: dict) -> str:
        if result is None:
            bit = args[0] if args else kwargs.get("ref") or kwargs.get("sheet_id") or ""
            return f"{op} {bit}".strip()
        if hasattr(result, "id") and hasattr(result, "title"):
            return f"{op} {result.id}  {result.title}"
        if hasattr(result, "id"):
            extra = getattr(result, "library_id", "") or getattr(result, "name", "")
            return f"{op} {result.id}" + (f"  {extra}" if extra else "")
        if isinstance(result, dict):
            sid = result.get("sheet") or result.get("id") or ""
            return f"{op} {sid}".strip()
        return op

    return fmt


_ACTIVITY_METHODS: dict[str, Any] = {
    "place": _summary_place,
    "place_from_bom": lambda s, r, a, k: f"BOM {r.get('id')} on {r.get('kind')}",
    "move": _summary_generic("move"),
    "wire": _summary_wire,
    "delete_component": _summary_generic("delete"),
    "duplicate_component": _summary_generic("duplicate"),
    "rotate_component": _summary_generic("rotate"),
    "delete_wire": _summary_generic("delete wire"),
    "set_wire_label": lambda s, r, a, k: f"label {r.id} {r.caption()}",
    "add_sheet": _summary_generic("add sheet"),
    "delete_sheet": _summary_generic("delete sheet"),
    "close_sheet": _summary_generic("close sheet"),
    "open_sheet": _summary_generic("open sheet"),
    "set_sheet_title": _summary_generic("rename sheet"),
    "set_active_sheet": _summary_generic("page"),
    "set_title_block": _summary_generic("title block"),
    "set_control_voltage": lambda s, r, a, k: f"control voltage {'' if r is None else r}",
    "set_logo": _summary_generic("logo"),
    "add_rail": _summary_generic("add rail"),
    "delete_rail": _summary_generic("delete rail"),
    "set_enclosure": _summary_generic("enclosure"),
    "layout_from_schematic": _summary_generic("pack panel"),
    "recommend_cabinet": lambda s, r, a, k: f"cabinet {r.get('width_mm')}×{r.get('height_mm')}×{r.get('depth_mm')}",
    "set_scale": _summary_generic("scale"),
    "set_display_size": lambda s, r, a, k: f"display {r.id} {r.properties.get('display_width_mm')}×{r.properties.get('display_height_mm')}",
    "set_picture": lambda s, r, a, k: f"picture {a[0] if a else k.get('ref')}",
    "clear_picture": _summary_generic("clear picture"),
    "apply_cut_sheet_pictures": lambda s, r, a, k: f"cut-sheet photos {r.get('applied', 0)}",
    "request_oem_update": lambda s, r, a, k: f"OEM update {r.get('count', '')} part(s)",
    "apply_oem": lambda s, r, a, k: f"OEM {r.get('id')}",
    "set_pins_locked": lambda s, r, a, k: f"{'lock' if r.pins_locked else 'unlock'} pins {r.id}",
    "set_pin": lambda s, r, a, k: f"slide pin {a[0] if a else k.get('ref')}.{a[1] if len(a) > 1 else k.get('pin_id')}",
    "reset_pins": _summary_generic("reset pins"),
    "set_panel": _summary_generic("panel dims"),
    "set_active_panel": lambda s, r, a, k: f"active panel {r.get('tag')}",
    "add_interconnect": lambda s, r, a, k: f"interconnect {r.get('tag')}",
    "confirm_envelope": _summary_generic("confirm envelope"),
    "assign_part": lambda s, r, a, k: f"catalog {r.get('manufacturer')} {r.get('catalog')} → {r.get('id')}",
    "set_control_power_margin": lambda s, r, a, k: f"control power margin {r.get('margin')}",
    "set_power_estimate": lambda s, r, a, k: f"power estimate {r.get('id')}",
    "set_available_fault": lambda s, r, a, k: (
        f"available fault {r.get('available_fault_ka')} kA"
    ),
    "set_part_sccr": lambda s, r, a, k: f"SCCR {k.get('ref') or (a[0] if a else '')}",
    "part_set_cutout": lambda s, r, a, k: f"cutout {r.get('id')}" + (" cleared" if r.get("cleared") else ""),
    "nameplate_set": lambda s, r, a, k: "nameplate",
    "set_address": _summary_generic("address"),
    "set_component_properties": _summary_generic("properties"),
    "add_port": _summary_generic("add port"),
    "set_port": _summary_generic("set port"),
    "delete_port": _summary_generic("delete port"),
    "set_network": _summary_generic("network"),
    "layout_network": _summary_generic("layout network"),
    "layout_io": _summary_generic("layout I/O"),
    "cleanup": _summary_generic("cleanup"),
    "set_pin_labels": _summary_generic("pin labels"),
    "wire_offpage": _summary_generic("off-page wire"),
    "set_wire_route": _summary_generic("route"),
    "clear_wire_route": _summary_generic("reset route"),
    "save": _summary_generic("save"),
    "import_bom": _summary_generic("import BOM"),
    "import_wires": _summary_generic("import wires"),
    "import_any": _summary_generic("import"),
    "import_library": _summary_generic("import library"),
    "number_wires": lambda s, r, a, k: f"number wires {r.get('assigned', 0)} ({r.get('scheme', '')})",
    "set_wire_numbering": lambda s, r, a, k: f"wire numbering {r.get('scheme')} auto={'on' if r.get('enabled') else 'off'}",
    "set_net_wire_spec": lambda s, r, a, k: (
        f"net spec {r.get('net')} {r.get('size')} {r.get('color')} {r.get('type')}".strip()
    ),
    "set_cross_reference": lambda s, r, a, k: (
        f"xref pitch {r.get('rung_pitch_mm')} mm  nc {r.get('nc_mark')}"
    ),
    "update_cross_references": lambda s, r, a, k: f"write cross-references {len(r) if isinstance(r, list) else ''}",
    "reconstruct_add_photos": lambda s, r, a, k: f"reconstruct photos +{len(r.get('added') or [])}",
    "reconstruct_tag_photo": lambda s, r, a, k: f"tag photo {r.get('id')} {r.get('role')}",
    "reconstruct_set_corners": lambda s, r, a, k: f"photo corners {r.get('id')}",
    "reconstruct_set_scale": lambda s, r, a, k: f"photo scale {r.get('photo')} {r.get('kind')}",
    "reconstruct_clear_scale": lambda s, r, a, k: f"clear photo scale {r.get('photo') or (a[0] if a else '')}",
    "reconstruct_identify": lambda s, r, a, k: f"identify photos {len(r.get('items') or [])}",
    "reconstruct_confirm_item": lambda s, r, a, k: f"confirm {r.get('id')} {r.get('status')}",
    "reconstruct_place_confirmed": lambda s, r, a, k: f"place confirmed {len(r.get('placed') or [])}",
    "reconstruct_place_needs_info": lambda s, r, a, k: f"place needs-info {len(r.get('placed') or [])}",
    "reconstruct_answer": lambda s, r, a, k: f"answer {r.get('id')}",
    "reconstruct_confirm_connection": lambda s, r, a, k: f"confirm wire {r.get('wire_id') or r.get('id')}",
    "reconstruct_attach_print": lambda s, r, a, k: f"attach print {r.get('id')}",
    "reconstruct_compare_print": lambda s, r, a, k: f"compare print {len(r.get('mismatches') or [])}",
    "set_wire_spec": lambda s, r, a, k: f"wire spec {getattr(r, 'id', '')}",
    "clear_logo": lambda s, r, a, k: "clear logo",
    "set_phase_colors": lambda s, r, a, k: "phase colors",
    "assign_item_numbers": lambda s, r, a, k: f"item numbers {r}",
    "sequence_terminal_strip": lambda s, r, a, k: f"sequence terminals {a[0] if a else k.get('strip', '')}",
    "import_print": lambda s, r, a, k: "import print",
    "import_sheet": lambda s, r, a, k: f"import sheet {r}",
}


# Save writes last-saved metadata and a recovery copy. That is not a drawing edit.
_SKIP_CHECKPOINT = frozenset({"save"})


def _install_activity_notes() -> None:
    for name, fmt in _ACTIVITY_METHODS.items():
        orig = getattr(Session, name)

        def _wrap(orig=orig, name=name, fmt=fmt):  # noqa: B023
            def inner(self: Session, *args: Any, **kwargs: Any) -> Any:
                tracked = name not in _SKIP_CHECKPOINT
                opened = self.begin_command(name) if tracked else False
                try:
                    result = orig(self, *args, **kwargs)
                finally:
                    if opened:
                        self.end_command()
                try:
                    self.emit(name, fmt(self, result, args, kwargs))
                except Exception:  # noqa: BLE001
                    pass
                return result

            inner.__name__ = orig.__name__
            inner.__doc__ = orig.__doc__
            return inner

        setattr(Session, name, _wrap())


_install_activity_notes()


def _headless_qt() -> bool:
    import os

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return True
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def prepare_qt_fonts() -> None:
    """Drop the font-directory warning without replacing the system fonts.

    ``QT_QPA_FONTDIR`` turns off the system font database. On Windows that
    selects a tighter face, and the update dialog grab fell to 114 px.
    """

    try:
        install_headless_qt_filter()
    except Exception:  # noqa: BLE001 — a headless import still runs without Qt
        return


_filter_installed = False


def install_headless_qt_filter() -> None:
    """Drop the PySide font-directory warning. A display does not make the warning useful.

    Install once. ``Session`` creation used to wrap the handler again, and a
    message that paints emits another message. That stack overflowed in the
    update dialog and in later Qt tests.
    """

    global _filter_installed
    if _filter_installed:
        return
    from PySide6.QtCore import qInstallMessageHandler

    previous = qInstallMessageHandler(None)
    depth = 0

    def _quiet(mode, context, message) -> None:  # noqa: ANN001
        nonlocal depth
        text = str(message or "")
        # Only this warning. Dropping every QFontDatabase message during
        # QApplication startup left later Windows tests with a doubled font.
        if "Cannot find font directory" in text:
            return
        if previous is None or depth:
            return
        depth += 1
        try:
            previous(mode, context, message)
        finally:
            depth -= 1

    qInstallMessageHandler(_quiet)
    _filter_installed = True


def _ensure_qt_app() -> None:
    import os

    prepare_qt_fonts()
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        # Headless CLI/PDF only. Never force offscreen while a GUI is already running —
        # that was blanking the on-screen title block after File → Print/PDF.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        install_headless_qt_filter()
        QApplication([])


def create_project(name: str) -> Session:
    return Session.create(name)


def open_project(path: Path | str) -> Session:
    return Session.open(path)
