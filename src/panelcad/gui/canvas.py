from __future__ import annotations

import math
from collections import OrderedDict

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QMenu,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.geometry import Point
from panelcad.core.layout import (
    is_io_sheet,
    is_layout_sheet,
    is_network_sheet,
    is_wire_by_wire_sheet,
)
from panelcad.core.models import PinOverride, infer_pin_side
from panelcad.core.route import move_wire_corner, nearest_run, set_run_channel
from panelcad.gui.canvas_items import (
    BOM_MIME as BOM_MIME,
    ComponentItem,
    EnclosureItem,
    FindingMarker,
    LIBRARY_MIME as LIBRARY_MIME,
    LogoSlotItem,
    MARKER_STANDOFF as MARKER_STANDOFF,
    PANEL_SNAP,
    PIN_DRAW_R as PIN_DRAW_R,
    PanelWiringOverlayItem,
    PhaseCalloutItem,
    PhaseLeadItem,
    PinItem,
    RailItem,
    RailJunctionItem,
    ResizeHandle as ResizeHandle,
    RevisionCloudItem,
    SNAP,
    SheetChromeItem,
    TitleBlockItem,
    TraceHalo,
    TraceWireMark,
    TrayItem,
    WIRE_HIT_MM as WIRE_HIT_MM,
    WIRE_HIT_PX as WIRE_HIT_PX,
    WireByWireSheetItem,
    WireHandleItem,
    WireItem,
    XrefLabelItem,
    _SheetScene,
    _bom_from_mime,
    _host_component,
    _library_id_from_mime,
    _paint_primitive as _paint_primitive,
    _pixels_per_scene,
    _problems_tab_current,
    _qc,
    _register_schematic_view_cls,
    _screen_hex as _screen_hex,
    _wire_screen_pen,
    device_drawn_scene_bounds,
    finding_content_rect as finding_content_rect,
    group_finding_rows,
    marker_pos_off_top_right,
    place_clear_of_devices,
    snap,
    wire_drawn_scene_bounds,
    wire_hit_radius_scene,
    wire_hit_width_scene as wire_hit_width_scene,
)
from panelcad.render.io_ladder import is_io_module
from panelcad.render.qt_painter import draw_world_text as draw_world_text
from panelcad.render.sheet import iter_wire_points
from panelcad.render.titleblock import (
    company_logo_rect,
    title_block_rect,
    working_rect,
)


class SchematicView(QGraphicsView):
    status = Signal(str)
    pin_clicked = Signal(str)
    selection_changed = Signal()
    logo_clicked = Signal()
    project_changed = Signal()
    finding_clicked = Signal(dict)
    object_edit_requested = Signal(str)
    rename_requested = Signal(str)
    address_edit_requested = Signal(str)
    wire_label_requested = Signal(str)
    wire_number_requested = Signal(str)
    wire_spec_requested = Signal(str)
    delete_sheet_requested = Signal()
    close_sheet_requested = Signal()
    new_sheet_requested = Signal()
    rename_sheet_requested = Signal()
    pack_requested = Signal()
    view_changed = Signal()

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.mode = "select"  # select | place | wire | cloud
        self.place_library_id: str | None = None
        self.wire_from: str | None = None
        self._wire_from_at: QPointF | None = None
        self._wire_drag = False
        self._wire_preview: QGraphicsPathItem | None = None
        self._cloud_origin: QPointF | None = None
        self._cloud_preview: QGraphicsPathItem | None = None
        self._zip_tie_origin: QPointF | None = None
        self._zip_tie_preview: QGraphicsPathItem | None = None
        self._slide_pin: PinItem | None = None
        self._slide_moved = False
        self._pull_wire: WireItem | None = None
        self._pull_pts: list[Point] | None = None
        self._pull_seg = -1
        self._pull_kind = ""
        self._pull_moved = False
        self._corner_wire: WireItem | None = None
        self._corner_pts: list[Point] | None = None
        self._corner_index = -1
        self._corner_moved = False
        self._wire_end_drag: dict | None = None
        self._wire_end_preview: QGraphicsPathItem | None = None
        self._wire_end_snap: QGraphicsEllipseItem | None = None
        self._wire_handles: list[WireHandleItem] = []
        self._syncing_wire_handles = False
        # PySide destroys QGraphicsItems when the last Python wrapper is GC'd.
        # Scene.addItem is not enough — keep every Python-created item here.
        self._items: list[QGraphicsItem] = []
        self._panning = False
        self._pan_start = QPointF()
        self._middle_press_pos: QPointF | None = None
        self._middle_hit_device: ComponentItem | None = None
        self._middle_hit_wire: WireItem | None = None
        self._left_press_pos: QPointF | None = None
        self._left_empty_press = False
        self._left_select_drag = False
        self._hover_wire: WireItem | None = None
        self._wire_hit_scale = -1.0
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Do not re-center on dock/inspector resize — that was scrolling the title block off-screen.
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self._moved_since_press = False
        self._dragged_ids: set[str] = set()
        self._gesture_edit = False
        self._closing_gesture = False
        self._applying_model_pos = False
        self._shown_sheet_id = ""
        self._sheet_scenes: OrderedDict[str, _SheetScene] = OrderedDict()
        self._spare_scenes: list[tuple[QGraphicsScene, list[QGraphicsItem]]] = []
        self._strip_caption = ""
        self._findings: list[dict[str, str]] = []
        self._trace_highlight = None
        self.setBackgroundBrush(QBrush(_qc("paper")))
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.scale(3.0, 3.0)
        self._user_zoomed = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_if_automatic)
        self.scene.selectionChanged.connect(self.selection_changed)
        self.selection_changed.connect(self._sync_wire_handles)
        self.reload()

    def _add(self, item: QGraphicsItem) -> QGraphicsItem:
        """Add *item* to the scene and hold a Python reference so PySide cannot GC it."""
        self._items.append(item)
        self.scene.addItem(item)
        return item

    def _device_tag_layouts(self) -> dict[str, tuple[float, float, str, float]]:
        """Same tag seats the sheet painter uses, so the screen matches the print."""

        from panelcad.core.ladder import sheet_drawing_style
        from panelcad.render.label_avoid import resolve_labels

        sheet = self.session.sheet()
        style = sheet_drawing_style(sheet, self.session.project)
        elementary = style in {"ladder", "iec"} and sheet.kind == "schematic"
        try:
            return resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=elementary,
            ).tags
        except (KeyError, ValueError, IndexError):
            return {}

    def reload(self) -> None:
        """Redraw the active sheet. A recent sheet is shown again without rebuilding.

        Only a few scenes stay in memory. The rest are released. A scene is
        reused only when the project, the library symbols, and the theme still
        match the picture that was built.
        """

        from panelcad.memory import scene_cache_slots
        from panelcad.render.label_avoid import share_resolved_labels
        from panelcad.render.wire_labels import share_wire_marks

        self._discard_preview()
        self._drop_deleted_sheets()
        sheet = self.session.sheet()
        stamp = self._scene_stamp()
        self._drop_stale_sheet_scenes(stamp)
        cached = self._sheet_scenes.get(sheet.id)
        if (
            cached is not None
            and cached.stamp == stamp
            and cached.scene is not None
            and self._cached_picture_matches(cached)
        ):
            self._mount_sheet_scene(cached)
            self._sheet_scenes.move_to_end(sheet.id)
            self._draw_finding_markers()
            self._draw_trace_highlight()
            self.status.emit(self._status_text())
            self.project_changed.emit()
            return
        self._prepare_build_scene(scene_cache_slots())
        with share_wire_marks(), share_resolved_labels():
            self._reload_sheet()
        self._remember_sheet_scene(sheet.id, stamp)
        self._evict_sheet_scenes(scene_cache_slots())

    def _scene_stamp(self) -> str:
        """Project stamp, reused while the drawing and the theme stay put.

        Hashing the whole project on every sheet change made a walk slower
        than clearing the one scene. The hash is taken again when an edit
        bumps the generation, the project object is replaced, or a symbol changes.
        """

        from panelcad.render.label_avoid import _digest, _library_key, project_drawing_stamp
        from panelcad.theme import get_canvas, get_theme

        project = self.session.project
        catalog = self.session.catalog
        theme = (get_theme(), get_canvas())
        props_sig = tuple((s.id, repr(sorted((s.properties or {}).items()))) for s in project.sheets)
        key = (
            int(getattr(self.session, "_drawing_generation", 0)),
            id(project),
            id(catalog),
            _digest(_library_key(project, catalog)),
            theme,
            props_sig,
        )
        if key == getattr(self, "_scene_stamp_key", None):
            return self._scene_stamp_value
        stamp = f"{project_drawing_stamp(project, catalog)}|{theme[0]}|{theme[1]}|{_digest(repr(props_sig))}"
        self._scene_stamp_key = key
        self._scene_stamp_value = stamp
        return stamp

    def _discard_preview(self) -> None:
        """Drop a rubber-band so a parked sheet does not keep it."""

        self._clear_wire_hover()
        self._cancel_wire_end_drag()
        self._clear_wire_handles()
        self._wire_hit_scale = -1.0
        for attr in ("_wire_preview", "_cloud_preview", "_wire_end_preview", "_wire_end_snap"):
            item = getattr(self, attr, None)
            if item is None:
                continue
            if item in self._items:
                self._items.remove(item)
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
            setattr(self, attr, None)
        self._cloud_origin = None

    def _scene_is_cached(self, scene: QGraphicsScene | None) -> bool:
        return any(slot.scene is scene for slot in self._sheet_scenes.values())

    def _prepare_build_scene(self, cap: int) -> None:
        """Keep a cached picture, and draw the next sheet on a scene we already own.

        Allocating a new scene for every sheet made switching slower than
        clearing the one scene. The pool stays at the cache cap.
        """

        if not self._scene_is_cached(self.scene) or cap <= 1:
            return
        if self._spare_scenes:
            scene, items = self._spare_scenes.pop()
            self.scene = scene
            self._items = items
            self.setScene(scene)
            return
        if len(self._sheet_scenes) >= cap:
            for sid, slot in self._sheet_scenes.items():
                if slot.scene is self.scene:
                    continue
                self._sheet_scenes.pop(sid)
                self.scene = slot.scene
                self._items = slot.items
                self.setScene(slot.scene)
                return
        scene = QGraphicsScene(self)
        scene.selectionChanged.connect(self.selection_changed)
        self.scene = scene
        self._items = []
        self.setScene(scene)

    def _mount_sheet_scene(self, slot: _SheetScene) -> None:
        self._items = slot.items
        self._shown_sheet_id = slot.sheet_id
        if self.scene is not slot.scene:
            self.scene = slot.scene
            self.setScene(slot.scene)
        # A full rebuild used to clear the scene, which dropped the selection.
        slot.scene.blockSignals(True)
        try:
            slot.scene.clearSelection()
        finally:
            slot.scene.blockSignals(False)
        self._clear_wire_handles()
        # The parked picture can still sit where a drag left it. The model wins.
        self._resync_items_from_model()

    def _release_mouse_grab(self) -> None:
        scene = self.scene
        if scene is None:
            return
        grabber = scene.mouseGrabberItem()
        if grabber is not None:
            grabber.ungrabMouse()

    def _resync_items_from_model(self) -> None:
        """Move the devices on the open scene to the coordinates in the model."""

        if self.scene is None:
            return
        try:
            sheet = self.session.sheet()
        except KeyError:
            return
        by_id = {comp.id: comp for comp in sheet.components}
        self._applying_model_pos = True
        try:
            for item in self.scene.items():
                if not isinstance(item, ComponentItem):
                    continue
                if item.sheet_id and item.sheet_id != sheet.id:
                    continue
                comp = by_id.get(item.component_id)
                if comp is None:
                    continue
                item.setPos(float(comp.x), float(comp.y))
                item._model_xy = (float(comp.x), float(comp.y))
        finally:
            self._applying_model_pos = False

    def settle_before_sheet_change(self) -> None:
        """Drop a half-finished drag before its scene is parked.

        The mouse button may still be down. Ungrabbing stops the later release
        from sliding the device again after the model has stayed put.

        The open undo command must close here too. Leaving ``_history_lock``
        open made later place / wire / property edits nest inside the drag and
        push nothing, so Ctrl+Z looked broken and bug reports showed
        ``undo steps: 0``.
        """

        self.drop_open_gesture()
        self._release_mouse_grab()
        self._resync_items_from_model()
        self._moved_since_press = False
        self._dragged_ids.clear()

    def _remember_sheet_scene(self, sheet_id: str, stamp: str) -> None:
        # Reusing the live scene means it no longer belongs to the sheet that
        # was on it. Drop that slot or a one-slot cache keeps both names.
        for sid, slot in list(self._sheet_scenes.items()):
            if slot.scene is self.scene:
                self._sheet_scenes.pop(sid, None)
        previous = self._sheet_scenes.pop(sheet_id, None)
        if previous is not None and previous.scene is not self.scene:
            self._release_sheet_scene(previous)
        self._sheet_scenes[sheet_id] = _SheetScene(sheet_id, stamp, self.scene, self._items)
        self._sheet_scenes.move_to_end(sheet_id)

    def _cached_picture_matches(self, slot: _SheetScene) -> bool:
        """A parked scene is reused only when its devices still sit on the model.

        A drag, a wizard cancel, or a direct edit can move the model without
        rebuilding the picture. The numbers on the model win.
        """

        sheet = self.session.sheet()
        if slot.sheet_id != sheet.id:
            return False
        by_id = {comp.id: comp for comp in sheet.components}
        devices = [item for item in slot.items if isinstance(item, ComponentItem)]
        if {item.component_id for item in devices} != set(by_id):
            return False
        for item in devices:
            comp = by_id[item.component_id]
            if abs(float(item.pos().x()) - float(comp.x)) > 0.05:
                return False
            if abs(float(item.pos().y()) - float(comp.y)) > 0.05:
                return False
        return True

    def _drop_deleted_sheets(self) -> None:
        """A deleted page does not keep an empty scene."""

        live = {sheet.id for sheet in self.session.project.sheets}
        removed = False
        for sid in [sid for sid in self._sheet_scenes if sid not in live]:
            slot = self._sheet_scenes.pop(sid)
            self._destroy_sheet_scene(slot)
            removed = True
        if removed:
            self._trim_spare_scenes()

    def _destroy_sheet_scene(self, slot: _SheetScene) -> None:
        scene = slot.scene
        items = slot.items
        slot.items = []
        slot.scene = None  # type: ignore[assignment]
        items.clear()
        if scene is None:
            return
        self._spare_scenes = [(kept, bag) for kept, bag in self._spare_scenes if kept is not scene]
        if scene is self.scene:
            return
        scene.clear()
        scene.deleteLater()

    def _trim_spare_scenes(self) -> None:
        while self._spare_scenes:
            scene, items = self._spare_scenes.pop()
            items.clear()
            if scene is self.scene:
                continue
            scene.clear()
            scene.deleteLater()

    def _drop_stale_sheet_scenes(self, stamp: str) -> None:
        stale = [sid for sid, slot in self._sheet_scenes.items() if slot.stamp != stamp]
        for sid in stale:
            slot = self._sheet_scenes.pop(sid)
            if slot.scene is self.scene:
                continue
            self._release_sheet_scene(slot)

    def _evict_sheet_scenes(self, cap: int) -> None:
        cap = max(1, int(cap))
        guard = len(self._sheet_scenes) + 1
        while len(self._sheet_scenes) > cap and guard:
            guard -= 1
            sid, slot = self._sheet_scenes.popitem(last=False)
            if slot.scene is self.scene:
                self._sheet_scenes[sid] = slot
                self._sheet_scenes.move_to_end(sid)
                if len(self._sheet_scenes) == 1:
                    return
                continue
            self._release_sheet_scene(slot)

    def release_scenes(self) -> None:
        """Drop every Python reference to scene items, then clear the scenes.

        Called when the window closes, before Qt frees the canvas. Same order
        as ``_release_sheet_scene``: lists first, then ``scene.clear()``, so
        no item wrapper outlives the C++ item it points at.
        """

        scenes: list[QGraphicsScene] = []

        def keep(scene) -> None:
            if scene is not None and all(scene is not known for known in scenes):
                scenes.append(scene)

        keep(self.scene)
        for slot in list(self._sheet_scenes.values()):
            keep(slot.scene)
            slot.items.clear()
            slot.scene = None  # type: ignore[assignment]
        self._sheet_scenes.clear()
        for scene, items in self._spare_scenes:
            keep(scene)
            items.clear()
        self._spare_scenes = []
        self._items.clear()
        self._wire_handles.clear()
        self._wire_end_drag = None
        self._wire_end_preview = None
        self._wire_end_snap = None
        self._corner_wire = None
        self._corner_pts = None
        self._pull_wire = None
        self._pull_pts = None
        self._wire_preview = None
        self._cloud_preview = None
        self._cloud_origin = None
        for scene in scenes:
            scene.blockSignals(True)
            scene.clear()

    def _release_sheet_scene(self, slot: _SheetScene) -> None:
        scene = slot.scene
        items = slot.items
        slot.items = []
        slot.scene = None  # type: ignore[assignment]
        if scene is None or scene is self.scene:
            items.clear()
            return
        items.clear()
        scene.clear()
        self._spare_scenes.append((scene, items))

    def _reload_sheet(self) -> None:
        self._clear_wire_hover()
        self._cancel_wire_end_drag()
        self._wire_hit_scale = -1.0
        self.setBackgroundBrush(QBrush(_qc("paper")))
        self._items.clear()
        self._wire_handles.clear()
        self._wire_preview = None
        self._wire_end_preview = None
        self._wire_end_snap = None
        self._cloud_preview = None
        self._cloud_origin = None
        self.scene.clear()
        sheet = self.session.sheet()
        self._shown_sheet_id = sheet.id
        w, h = sheet.page.dimensions()
        self.scene.setSceneRect(0, 0, w, h)
        self._draw_grid(sheet)
        self._add(SheetChromeItem(self.session))
        self._add(TitleBlockItem(self.session))
        self._add(LogoSlotItem(company_logo_rect(sheet), self.logo_clicked.emit))
        self._zip_tie_origin = None
        self._zip_tie_preview = None
        if is_wire_by_wire_sheet(sheet):
            self._add(WireByWireSheetItem(self.session))
            self._draw_revision_clouds()
            self._draw_trace_highlight()
            self.status.emit(self._status_text())
            self.project_changed.emit()
            return
        if sheet.kind == "terminal":
            self._draw_terminal_strip(sheet)
            self._draw_revision_clouds()
            self._draw_trace_highlight()
            self.status.emit(self._status_text())
            self.project_changed.emit()
            return
        layout = is_layout_sheet(sheet)
        network = is_network_sheet(sheet)
        io_page = is_io_sheet(sheet)
        mm_scale = 1.0
        if sheet.enclosure is not None:
            self._add(EnclosureItem(sheet.enclosure))
            if sheet.enclosure.draw_scale and sheet.enclosure.draw_scale > 0:
                mm_scale = float(sheet.enclosure.draw_scale)
        for tray in sheet.trays:
            self._add(TrayItem(tray, mm_scale=mm_scale))
        for rail in sheet.rails:
            self._add(RailItem(rail, mm_scale=mm_scale))
        tags = self._device_tag_layouts() if sheet.kind == "schematic" and not layout and not network else {}
        for comp in sheet.components:
            device = self.session.catalog.get(comp.library_id)
            item = ComponentItem(
                comp,
                device,
                layout=layout,
                network=network,
                io_card=io_page and is_io_module(device) and not comp.properties.get("ladder_card"),
                sheet_id=sheet.id,
            )
            if comp.id in tags:
                item.set_tag_layout(tags[comp.id])
            self._add(item)
        if layout:
            self._add(PanelWiringOverlayItem(self.session))
        else:
            self._redraw_wires()
        self._apply_pin_mode()
        self._draw_finding_markers()
        self._draw_revision_clouds()
        self._draw_trace_highlight()
        self.status.emit(self._status_text())
        self.project_changed.emit()

    def _status_text(self) -> str:
        sheet = self.session.sheet()
        extra = ""
        if self.mode == "place" and self.place_library_id:
            extra = f"  ·  PLACE {self.place_library_id} (click empty sheet or drop from library)"
        elif self.mode == "cloud":
            extra = "  ·  CLOUD  drag a rectangle (Esc cancels)"
        elif self.wire_from:
            extra = f"  ·  wiring from {self.wire_from} — click or release on the other terminal or rail (Esc cancels)"
        elif self._wire_end_drag is not None:
            extra = "  ·  dragging wire end — drop on a valid pin or rail (Esc cancels)"
        elif self._pull_wire is not None or self._corner_wire is not None:
            extra = "  ·  dragging wire — release to keep, Esc to cancel"
        if is_layout_sheet(sheet):
            return (
                f"{sheet.title}  ·  panel layout  ·  {len(sheet.components)} devices  ·  "
                f"{len(sheet.rails)} rails  ·  select a part, drag the orange handle to resize{extra}"
            )
        if is_network_sheet(sheet):
            medium = sheet.network.medium if sheet.network else "mixed"
            return (
                f"{sheet.title}  ·  comms {medium}  ·  {len(sheet.components)} nodes  ·  "
                f"{len(sheet.wires)} links  ·  double-click a node to edit ports{extra}"
            )
        if is_io_sheet(sheet):
            return (
                f"{sheet.title}  ·  I/O ladder  ·  {len(sheet.components)} devices  ·  "
                f"{len(sheet.wires)} rungs  ·  right-click a wire to label it{extra}"
            )
        if sheet.kind == "terminal":
            note = self._strip_caption or "terminal strip"
            return f"{sheet.title}  ·  {note}{extra}"
        return (
            f"{sheet.title}  ·  {len(sheet.components)} devices  ·  "
            f"{len(sheet.wires)} wires  ·  drag a wire run to pull it  ·  "
            f"hover a terminal and click or drag to the next one{extra}"
        )

    def _draw_grid(self, sheet) -> None:
        wx, wy, ww, wh = working_rect(sheet)
        minor = QPen(_qc("grid"), 0.12)
        major = QPen(_qc("grid_major"), 0.22)
        x = wx
        index = 0
        while x <= wx + ww + 0.01:
            line = self.scene.addLine(x, wy, x, wy + wh, major if index % 5 == 0 else minor)
            _ignore_mouse(line)
            self._items.append(line)
            x += 10
            index += 1
        y = wy
        index = 0
        while y <= wy + wh + 0.01:
            line = self.scene.addLine(wx, y, wx + ww, y, major if index % 5 == 0 else minor)
            _ignore_mouse(line)
            self._items.append(line)
            y += 10
            index += 1

    def refresh_frame(self) -> None:
        found_block = False
        found_chrome = False
        for item in list(self.scene.items()):
            if isinstance(item, TitleBlockItem):
                item.update()
                found_block = True
            elif isinstance(item, SheetChromeItem):
                item.update()
                found_chrome = True
        if not found_chrome:
            self._add(SheetChromeItem(self.session))
        if not found_block:
            self._add(TitleBlockItem(self.session))

    def _redraw_wires(self) -> None:
        from panelcad.render.wire_labels import share_wire_marks

        with share_wire_marks():
            self._redraw_wire_items()

    def _redraw_wire_items(self) -> None:
        self._clear_wire_hover()
        self._wire_hit_scale = -1.0
        self._wire_handles.clear()
        kept: list[QGraphicsItem] = []
        for item in self._items:
            if isinstance(item, (WireItem, WireHandleItem, PhaseLeadItem, PhaseCalloutItem, RailJunctionItem)):
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
            else:
                kept.append(item)
        self._items = kept
        sheet = self.session.sheet()
        created: list[WireItem] = []
        from panelcad.core.ladder import sheet_drawing_style

        style = sheet_drawing_style(sheet, self.session.project)
        ladder = style == "ladder" and sheet.kind == "schematic"
        column = style == "iec" and sheet.kind == "schematic"
        elementary = ladder or column
        from panelcad.core.phase_color import wire_phase_pens
        from panelcad.render.crossings import hop_routes
        from panelcad.render.phase_ink import line_side_callouts, line_side_runs, pole_runs
        from panelcad.render.label_avoid import resolve_labels
        from panelcad.render.wiring import route_sheet_wires

        try:
            resolved = resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=elementary,
            )
            routes = resolved.routes
            marks = resolved.marks
        except (KeyError, ValueError, IndexError):
            resolved = None
            try:
                routes = route_sheet_wires(sheet, self.session.catalog, project=self.session.project)
            except (KeyError, ValueError, IndexError):
                routes = {}
            from panelcad.render.wire_labels import place_wire_marks

            marks = place_wire_marks(
                sheet, self.session.catalog, routes, ladder=elementary, project=self.session.project
            )
        pens = wire_phase_pens(self.session.project, sheet, self.session.catalog)
        try:
            leads = [
                *line_side_runs(self.session.project, sheet, self.session.catalog),
                *pole_runs(self.session.project, sheet, self.session.catalog),
            ]
        except (KeyError, ValueError, IndexError):
            leads = []
        for run in leads:
            self._add(
                PhaseLeadItem(
                    run.points,
                    run.pen.stroke,
                    run.pen.width,
                    outline=run.pen.outline,
                    outline_width=run.pen.outline_width,
                )
            )
        try:
            callouts = line_side_callouts(self.session.project, sheet, self.session.catalog)
        except (KeyError, ValueError, IndexError):
            callouts = []
        for callout in callouts:
            if callout.cap is not None:
                self._add(
                    PhaseLeadItem(
                        list(callout.cap),
                        callout.pen.stroke,
                        callout.pen.width,
                        outline=callout.pen.outline,
                        outline_width=callout.pen.outline_width,
                    )
                )
            self._add(
                PhaseCalloutItem(
                    callout.text,
                    callout.x,
                    callout.y,
                    callout.size,
                    callout.align,
                    callout.pen.stroke,
                    outline=callout.pen.outline,
                )
            )
        hopped = hop_routes(routes)
        for wire in sheet.wires:
            raw = routes.get(wire.id)
            if raw is None:
                try:
                    raw = iter_wire_points(
                        sheet, self.session.catalog, wire, project=self.session.project
                    )
                except (KeyError, ValueError, IndexError):
                    continue
            pts_xy = hopped.get(wire.id) or [(p.x, p.y) for p in raw]
            if not pts_xy:
                continue
            path = QPainterPath()
            path.moveTo(pts_xy[0][0], pts_xy[0][1])
            for x, y in pts_xy[1:]:
                path.lineTo(x, y)
            at = marks.get(wire.id)
            # Print draws nothing when the mark has no seat. A midpoint
            # fallback was painting 107/108/109 that the sheet does not show.
            # Print draws the seated mark and nothing when the mark has no seat.
            # Blanking every elementary caption hid 201–209 on the control sheet.
            caption = "" if at is None else wire.drawn_mark(ladder=elementary)
            pen = pens.get(wire.id)
            item = WireItem(
                path,
                wire.id,
                caption,
                label_xy=(at[0], at[1]) if at else None,
                label_align=at[2] if at else "start",
            )
            if pen is not None:
                from panelcad.theme import adapt_print_color

                outline = QColor(adapt_print_color(pen.outline)) if pen.outline else None
                item.set_screen_pen(QColor(adapt_print_color(pen.stroke)), pen.width, outline)
            else:
                item.set_screen_pen(*_wire_screen_pen(wire, self.session, ladder=elementary))
            if getattr(wire, "inferred", False):
                dashed = QPen(item.pen())
                dashed.setStyle(Qt.PenStyle.DashLine)
                item.setPen(dashed)
                item._base_pen = dashed
            tip = f"Wire {wire.id}  {wire.frm} → {wire.to}"
            if getattr(wire, "inferred", False):
                tip = f"INFERRED (not confirmed)  {tip}"
            if caption:
                tip += f"  [{caption}]"
            tip += "\nDrag a run to pull it. Right-click to label, reset route, or delete."
            item.setToolTip(tip)
            created.append(item)
        for item in created:
            self._add(item)
        from panelcad.core.ladder import rail_landing_points

        for jx, jy in rail_landing_points(sheet, routes, self.session.project):
            self._add(RailJunctionItem(jx, jy))
        self._redraw_xref(ladder=elementary)
        self._sync_wire_handles()

    def _redraw_xref(self, *, ladder: bool = False) -> None:
        kept: list[QGraphicsItem] = []
        for item in self._items:
            if isinstance(item, XrefLabelItem):
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
            else:
                kept.append(item)
        self._items = kept
        sheet = self.session.sheet()
        if sheet.kind not in {"schematic", "io"}:
            return
        from panelcad.render.label_avoid import resolve_labels

        try:
            placed = resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=ladder,
            ).xrefs
        except (KeyError, ValueError, IndexError):
            return
        for ann, x, y, align in placed:
            self._add(XrefLabelItem(ann, x, y, align))

    def _apply_pin_mode(self) -> None:
        layout = is_layout_sheet(self.session.sheet())
        buttons = Qt.MouseButton.NoButton if layout else Qt.MouseButton.LeftButton
        sheet = self.session.sheet()
        for item in self.scene.items():
            if isinstance(item, PinItem):
                item.setAcceptedMouseButtons(buttons)
                try:
                    item.set_locked(sheet.component(item.component_id).pins_locked)
                except KeyError:
                    item.set_locked(True)

    def _pin_item(self, ref: str) -> PinItem | None:
        for item in self.scene.items():
            if isinstance(item, PinItem) and item.ref() == ref:
                return item
        return None

    def _clear_wire_preview(self) -> None:
        if self._wire_preview is not None:
            self.scene.removeItem(self._wire_preview)
            self._wire_preview = None

    def _update_wire_preview(
        self,
        scene_pos: QPointF,
        snap_pin: PinItem | None = None,
        snap_at: QPointF | None = None,
    ) -> None:
        if not self.wire_from:
            self._clear_wire_preview()
            return
        if self._wire_from_at is not None:
            ax, ay = self._wire_from_at.x(), self._wire_from_at.y()
        else:
            start = self._pin_item(self.wire_from)
            if start is None:
                self._clear_wire_preview()
                return
            ax, ay = start.scenePos().x(), start.scenePos().y()
        if snap_pin is not None:
            bx, by = snap_pin.scenePos().x(), snap_pin.scenePos().y()
        elif snap_at is not None:
            bx, by = snap_at.x(), snap_at.y()
        else:
            bx, by = scene_pos.x(), scene_pos.y()
        path = QPainterPath()
        path.moveTo(ax, ay)
        mid = (ax + bx) / 2.0
        path.lineTo(mid, ay)
        path.lineTo(mid, by)
        path.lineTo(bx, by)
        if self._wire_preview is None:
            self._wire_preview = QGraphicsPathItem()
            self._wire_preview.setPen(QPen(_qc("wire_preview"), 0.55, Qt.PenStyle.DashLine))
            self._wire_preview.setZValue(19)
            self._wire_preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene.addItem(self._wire_preview)
        self._wire_preview.setPath(path)

    def _drop_gesture_flags(self) -> None:
        """Forget a drag without touching the undo stack."""

        self._gesture_edit = False
        self._slide_pin = None
        self._slide_moved = False
        self._pull_wire = None
        self._pull_pts = None
        self._pull_seg = -1
        self._pull_kind = ""
        self._pull_moved = False
        self._corner_wire = None
        self._corner_pts = None
        self._corner_index = -1
        self._corner_moved = False
        self._cancel_wire_end_drag(resync=False)
        self._moved_since_press = False
        self._dragged_ids.clear()
        self.unsetCursor()

    def drop_open_gesture(self) -> None:
        """Cancel a drag still under the cursor. Do not record it as an undo step.

        Clears the canvas flags and abandons the session command so later edits
        do not nest inside a half-finished gesture.
        """

        if self._gesture_edit or self.session.command_open():
            self.session.abandon_edit()
        self._drop_gesture_flags()

    def _finish_gesture(self, *, moved: bool = True) -> None:
        if not self._gesture_edit:
            return
        self._gesture_edit = False
        if not self.session.command_open():
            # Undo/Redo already abandoned this drag.
            return
        self.session.end_edit()
        if not moved:
            self.session.forget_noop_checkpoint()

    def _close_open_gesture(self) -> None:
        """End a drag that never received a mouse release.

        A second press, or the canvas losing focus or the mouse grab, used to
        leave the step open. Later edits then nested inside it and pushed nothing.
        """

        if self._closing_gesture:
            return
        if self._wire_end_drag is not None:
            self._cancel_wire_end_drag()
        if not self._gesture_edit:
            # Flags cleared without closing the session command (sheet switch
            # used to do this). Abandon so the next edit can push again.
            if self.session.command_open():
                self.session.abandon_edit()
            return
        self._closing_gesture = True
        try:
            self._close_open_gesture_body()
        finally:
            self._closing_gesture = False

    def _close_open_gesture_body(self) -> None:
        if not self.session.command_open():
            self._drop_gesture_flags()
            return
        if self._corner_wire is not None:
            moved = self._corner_moved
            self._commit_wire_corner_drag()
            self._finish_gesture(moved=moved)
            return
        if self._pull_wire is not None:
            moved = self._pull_moved
            self._commit_wire_pull()
            self._finish_gesture(moved=moved)
            return
        if self._slide_pin is not None:
            pin = self._slide_pin
            moved = self._slide_moved
            self._slide_pin = None
            self._slide_moved = False
            if moved:
                self._commit_slide(pin)
            self._finish_gesture(moved=moved)
            return
        user_moved = bool(self._moved_since_press and self._dragged_ids)
        moved = self._commit_symbol_motion(lift=False)
        self._finish_gesture(moved=bool(moved or user_moved))
        self._moved_since_press = False
        self._dragged_ids.clear()

    def _begin_gesture(self, name: str) -> None:
        self._close_open_gesture()
        self.session.begin_edit(name)
        self._gesture_edit = True

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._close_open_gesture()
        super().focusOutEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.FocusOut):
            self._close_open_gesture()
        return super().viewportEvent(event)

    def cancel_wire(self) -> None:
        """Clear an in-progress wire. Does not cancel a symbol move.

        ``set_select_mode`` calls this while a device is being dragged. Dropping
        the move command here used to wipe the open undo step mid-drag.
        """

        end_dragging = self._wire_end_drag is not None
        if end_dragging:
            self._cancel_wire_end_drag()
        pulling = self._pull_wire is not None or self._corner_wire is not None
        sliding = self._slide_pin is not None
        # Only abandon pull / corner / slide. A plain move keeps its open command.
        if pulling or sliding:
            dragged_wire_id = (
                self._pull_wire.wire_id
                if self._pull_wire is not None
                else (self._corner_wire.wire_id if self._corner_wire is not None else "")
            )
            self.drop_open_gesture()
            self._resync_items_from_model()
            self._redraw_wires()
            if dragged_wire_id:
                self._select_wire_item(dragged_wire_id)
        else:
            self._slide_pin = None
            self._slide_moved = False
            self._pull_wire = None
            self._pull_pts = None
            self._pull_seg = -1
            self._pull_kind = ""
            self._pull_moved = False
            self._corner_wire = None
            self._corner_pts = None
            self._corner_index = -1
            self._corner_moved = False
        self.wire_from = None
        self._wire_from_at = None
        self._wire_drag = False
        self._clear_wire_preview()
        self.status.emit(self._status_text())

    def _sheet_for_item(self, item: ComponentItem):
        """Sheet this symbol was placed on. The active tab is not that identity."""

        sid = str(getattr(item, "sheet_id", "") or getattr(self, "_shown_sheet_id", "") or "")
        if not sid:
            return self.session.sheet()
        try:
            return self.session.project.sheet(sid)
        except KeyError:
            return None

    def sync_positions(self) -> set[str]:
        """Copy a dragged symbol onto the instance it was built for.

        The same tag on another page is a different placement. A scene item
        writes only that placement, never the active sheet's copy of the tag.

        The scene wins only while the pointer is dragging that symbol. An edit
        that arrived in the model — a ``panelcad send`` move, undo, or another
        page — refreshes the item. Writing the stale scene back is what put an
        on-screen device back where it started.
        The drawing grid applies only to symbols the pointer has dragged.
        Rounding every item pulled an imported, off-grid layout onto the grid
        the first time someone clicked a device.
        """

        snap_ids = set(self._dragged_ids) if self._moved_since_press else set()
        moved: set[str] = set()
        active_id = self.session.active_sheet_id
        for item in self.scene.items():
            if not isinstance(item, ComponentItem):
                continue
            sheet = self._sheet_for_item(item)
            if sheet is None:
                continue
            try:
                comp = sheet.component(item.component_id)
            except KeyError:
                continue
            scene_x = float(item.pos().x())
            scene_y = float(item.pos().y())
            built = getattr(item, "_model_xy", None)
            if not built:
                built = (scene_x, scene_y)
            built_x, built_y = float(built[0]), float(built[1])
            model_x, model_y = float(comp.x), float(comp.y)
            scene_moved = abs(scene_x - built_x) > 1e-6 or abs(scene_y - built_y) > 1e-6
            model_moved = abs(model_x - built_x) > 1e-6 or abs(model_y - built_y) > 1e-6
            # Only a drag owns the scene. Anything else, including a command
            # that moved the model while this picture was still on screen,
            # keeps the model and the picture catches up.
            if scene_moved and item.component_id in snap_ids:
                nx, ny = scene_x, scene_y
                step = item._snap
                nx = round(nx / step) * step
                ny = round(ny / step) * step
                if abs(model_x - nx) > 1e-6 or abs(model_y - ny) > 1e-6:
                    comp.x = nx
                    comp.y = ny
                    if sheet.id == active_id:
                        moved.add(item.component_id)
                item._model_xy = (float(comp.x), float(comp.y))
                if abs(scene_x - comp.x) > 1e-6 or abs(scene_y - comp.y) > 1e-6:
                    self._applying_model_pos = True
                    try:
                        item.setPos(comp.x, comp.y)
                    finally:
                        self._applying_model_pos = False
            elif scene_moved or model_moved:
                self._applying_model_pos = True
                try:
                    item.setPos(model_x, model_y)
                finally:
                    self._applying_model_pos = False
                item._model_xy = (model_x, model_y)
        return moved

    def _sync_scene_from_model(self, ids: set[str]) -> None:
        if not ids:
            return
        sheet = self.session.sheet()
        by_id = {c.id: c for c in sheet.components}
        self._applying_model_pos = True
        try:
            for item in self.scene.items():
                if not isinstance(item, ComponentItem):
                    continue
                if str(getattr(item, "sheet_id", "") or "") not in {"", sheet.id}:
                    continue
                if item.component_id not in ids or item.component_id not in by_id:
                    continue
                comp = by_id[item.component_id]
                item.setPos(comp.x, comp.y)
                item._model_xy = (float(comp.x), float(comp.y))
        finally:
            self._applying_model_pos = False

    def _commit_symbol_motion(self, *, lift: bool) -> set[str]:
        """Store a drag. A click that never moved returns nothing and writes nothing."""

        dragged = set(self._dragged_ids)
        if not self._moved_since_press or not dragged:
            return set()
        moved = self.sync_positions()
        if lift and self._gesture_edit:
            lifted = set(self.session.lift_symbols_off_title_block(dragged))
            if lifted:
                self._sync_scene_from_model(lifted)
                moved.update(lifted)
        if is_layout_sheet(self.session.sheet()):
            self.session.snap_layout_positions(refs=dragged)
            self._sync_scene_from_model(dragged)
        elif self._moved_since_press:
            # The move handler already wrote the snapped position, so
            # sync_positions often returns nothing. Stored waypoints still
            # have to drop or the wires keep the old trace.
            affected = set(moved) | dragged
            if affected:
                self.session.drop_wire_routes(affected)
            self._redraw_wires()
            return affected
        return moved

    def _wires_follow_component(self, item: ComponentItem) -> None:
        sheet = self._sheet_for_item(item)
        if sheet is None or is_layout_sheet(sheet):
            return
        if sheet.id != self.session.sheet().id:
            return
        try:
            comp = sheet.component(item.component_id)
        except KeyError:
            return
        step = item._snap
        nx = round(item.pos().x() / step) * step
        ny = round(item.pos().y() / step) * step
        if abs(comp.x - nx) < 1e-6 and abs(comp.y - ny) < 1e-6:
            return
        comp.x, comp.y = nx, ny
        # Full routing waits for mouse release. A straight segment keeps the
        # wire on the pin until then. Release drops stored waypoints and reroutes.
        self._preview_drag_wires(item.component_id)

    def _preview_drag_wires(self, comp_id: str) -> None:
        """Straight stand-in while a device moves. Release replaces it with the route.

        The ends are the same points the router will use: a pin, a rung on a
        ladder rail, a clicked landing, or the horizontal IEC rail. The middle
        of a vertical rail is not a landing.
        """

        from panelcad.render.wiring import wire_ends

        sheet = self.session.sheet()
        catalog = self.session.catalog
        project = self.session.project
        by_id = {wire.id: wire for wire in sheet.wires}
        for item in self._items:
            if not isinstance(item, WireItem) or not item.wire_id:
                continue
            wire = by_id.get(item.wire_id)
            if wire is None:
                continue
            ends = (str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0])
            if comp_id not in ends:
                continue
            # Release drops stored waypoints and keeps a clicked rail landing.
            # The stand-in uses that same pair of ends, so an old riser does
            # not hold the rail off the pin's column.
            saved_points = list(wire.points)
            try:
                if saved_points:
                    wire.points = []
                ax, ay, _a_side, bx, by, _b_side = wire_ends(sheet, catalog, wire, project)
            except (KeyError, ValueError, IndexError):
                continue
            finally:
                if saved_points:
                    wire.points = saved_points
            path = QPainterPath()
            path.moveTo(ax, ay)
            path.lineTo(bx, by)
            item.setPath(path)

    def place_at(self, library_id: str, x: float, y: float) -> None:
        step = PANEL_SNAP if is_layout_sheet(self.session.sheet()) else SNAP
        self.session.begin_edit("place")
        try:
            self.sync_positions()
            try:
                comp = self.session.place(library_id, x=round(x / step) * step, y=round(y / step) * step)
            except ValueError as exc:
                self.status.emit(str(exc))
                return
            lifted = comp.id in self.session.lift_symbols_off_title_block([comp.id])
        finally:
            self.session.end_edit()
        if lifted:
            comp = self.session.sheet().component(comp.id)
        device = self.session.catalog.get(comp.library_id)
        item = ComponentItem(
            comp,
            device,
            layout=is_layout_sheet(self.session.sheet()),
            network=is_network_sheet(self.session.sheet()),
            io_card=is_io_sheet(self.session.sheet()) and is_io_module(device) and not comp.properties.get("ladder_card"),
            sheet_id=self.session.sheet().id,
        )
        if self.session.sheet().kind == "schematic" and not item.layout and not item.network:
            laid = self._device_tag_layouts().get(comp.id)
            if laid is not None:
                item.set_tag_layout(laid)
        self._add(item)
        self.scene.clearSelection()
        item.setSelected(True)
        self._apply_pin_mode()
        note = " above the title block" if lifted else ""
        self.status.emit(f"placed {comp.id}{note}  —  drag to move, Delete to remove")
        self.project_changed.emit()

    def place_from_bom_at(self, library_id: str, ref: str | None, x: float, y: float) -> None:
        step = PANEL_SNAP if is_layout_sheet(self.session.sheet()) else SNAP
        self.session.begin_edit("place")
        try:
            self.sync_positions()
            try:
                result = self.session.place_from_bom(
                    library_id,
                    ref=ref,
                    x=round(x / step) * step,
                    y=round(y / step) * step,
                )
            except ValueError as exc:
                self.status.emit(str(exc))
                return
            if result.get("placed"):
                self.session.lift_symbols_off_title_block([str(result["id"])])
        finally:
            self.session.end_edit()
        if not result.get("placed"):
            self.session.forget_noop_checkpoint()
        if not result.get("placed") and result.get("reason") == "not_layout_ready":
            self.status.emit(str(result.get("message") or result.get("reason")))
            return
        self.reload()
        self.select_ref(result["id"])
        if result.get("placed"):
            self.status.emit(
                f"BOM {result['id']} on {result['kind']}  ({result['library_id']})"
            )
        else:
            self.status.emit(f"{result['id']} already on this {result['kind']} sheet")
        self.project_changed.emit()

    def _to_point(self, view_pos) -> QPoint:
        if isinstance(view_pos, QPoint):
            return view_pos
        if hasattr(view_pos, "toPoint"):
            return view_pos.toPoint()
        return QPoint(int(view_pos.x()), int(view_pos.y()))

    def _device_at(self, view_pos) -> ComponentItem | None:
        for item in self.items(self._to_point(view_pos)):
            if isinstance(item, ComponentItem) or getattr(item, "strip_block", False):
                return item
            parent = item.parentItem()
            if isinstance(parent, ComponentItem) or getattr(parent, "strip_block", False):
                return parent
        return None

    def _pin_at(self, view_pos) -> PinItem | None:
        for item in self.items(self._to_point(view_pos)):
            if isinstance(item, PinItem):
                return item
        return None

    def _wire_at(self, view_pos) -> WireItem | None:
        # Walk the conductors themselves. A sheet frame, grid line, or rung
        # symbol can sit on the same pixel; the closest wire within the
        # screen-pixel hit still wins.
        scene_pos = self.mapToScene(self._to_point(view_pos))
        return self.wire_at_scene(scene_pos)

    def _refresh_wire_hit_geometry(self) -> None:
        scale = _pixels_per_scene(self)
        if abs(scale - self._wire_hit_scale) < 1e-9:
            return
        self._wire_hit_scale = scale
        for item in self._items:
            if isinstance(item, (WireItem, WireHandleItem)):
                item.prepareGeometryChange()
                item.update()

    def wire_at_scene(self, scene_pos: QPointF) -> WireItem | None:
        """Closest wire whose screen hit contains ``scene_pos``, or None."""

        self._refresh_wire_hit_geometry()
        limit = wire_hit_radius_scene(self)
        best: WireItem | None = None
        best_d = limit
        px, py = float(scene_pos.x()), float(scene_pos.y())
        point = QPointF(px, py)
        # scene.items() is topmost-first. Equal distances keep that wire.
        # _items is insertion order, which picked the bottom wire on a shared bus.
        for item in self.scene.items():
            if not isinstance(item, WireItem) or not item.isVisible():
                continue
            local = item.mapFromScene(point)
            # boundingRect already covers the hit pad and the label. A tight
            # path box dropped clicks that still land inside that pad at zoom 1.
            if not item.boundingRect().adjusted(-0.5, -0.5, 0.5, 0.5).contains(local):
                continue
            distance = item.distance_to_scene(px, py)
            if distance > limit + 1e-6:
                continue
            closer = best is None or distance < best_d - 1e-6
            tie = best is not None and abs(distance - best_d) <= 1e-6 and item.zValue() > best.zValue()
            if closer or tie:
                best = item
                best_d = distance
        return best

    def _hover_wire_at(self, view_pos) -> WireItem | None:
        scene_pos = self.mapToScene(self._to_point(view_pos))
        if self._wire_handle_at(view_pos, scene_pos) is not None:
            return None
        if self._pin_at(view_pos) is not None:
            return None
        if self._in_title_block(scene_pos):
            return None
        wiring = self.mode == "wire" or self.wire_from is not None
        if wiring and not is_layout_sheet(self.session.sheet()) and self._rail_at(scene_pos) is not None:
            return None
        return self.wire_at_scene(scene_pos)

    def _clear_wire_hover(self) -> None:
        item = self._hover_wire
        self._hover_wire = None
        if item is None:
            return
        try:
            if item.scene() is not None:
                item.set_hovered(False)
        except RuntimeError:
            return

    def _set_wire_hover(self, item: WireItem | None) -> None:
        if item is self._hover_wire:
            return
        self._clear_wire_hover()
        self._hover_wire = item
        if item is not None:
            item.set_hovered(True)

    def _wire_handle_at(self, view_pos, scene_pos: QPointF) -> WireHandleItem | None:
        px, py = float(scene_pos.x()), float(scene_pos.y())
        best: WireHandleItem | None = None
        best_d = 1e9
        for item in self.items(self._to_point(view_pos)):
            if not isinstance(item, WireHandleItem) or not item.isVisible():
                continue
            d = math.hypot(px - float(item.pos().x()), py - float(item.pos().y()))
            if best is None or d < best_d - 1e-6 or (abs(d - best_d) <= 1e-6 and item.zValue() > best.zValue()):
                best = item
                best_d = d
        return best

    def _clear_wire_handles(self) -> None:
        handles = list(self._wire_handles)
        self._wire_handles.clear()
        for handle in handles:
            if handle in self._items:
                self._items.remove(handle)
            try:
                if handle.scene() is self.scene and self.scene is not None:
                    self.scene.removeItem(handle)
            except RuntimeError:
                continue

    def _sync_wire_handles(self) -> None:
        if self._syncing_wire_handles or self.scene is None:
            return
        self._syncing_wire_handles = True
        try:
            self._clear_wire_handles()
            try:
                sheet = self.session.sheet()
            except KeyError:
                return
            if is_layout_sheet(sheet) or sheet.kind == "terminal":
                return
            if self._wire_end_drag is not None:
                return
            if self._pull_wire is not None and self._pull_moved:
                return
            if self._corner_wire is not None and self._corner_moved:
                return
            selected = [
                item
                for item in self.scene.selectedItems()
                if isinstance(item, WireItem) and item.wire_id and item.scene() is self.scene
            ]
            for wire_item in selected:
                try:
                    pts = self._wire_pts(wire_item.wire_id)
                except (KeyError, ValueError, IndexError):
                    continue
                if len(pts) < 2:
                    continue
                specs: list[tuple[float, float, str, str, int]] = [
                    (pts[0].x, pts[0].y, "end", "from", -1),
                    (pts[-1].x, pts[-1].y, "end", "to", -1),
                ]
                for idx in range(1, len(pts) - 1):
                    specs.append((pts[idx].x, pts[idx].y, "corner", "", idx))
                for hx, hy, kind, which, cidx in specs:
                    handle = WireHandleItem(
                        wire_item.wire_id,
                        hx,
                        hy,
                        kind=kind,
                        which=which,
                        corner_index=cidx,
                    )
                    self._wire_handles.append(handle)
                    self._add(handle)
        finally:
            self._syncing_wire_handles = False

    def _select_wire_item(self, wire_id: str) -> None:
        if self.scene is None or not wire_id:
            return
        for item in self._items:
            if isinstance(item, WireItem) and item.wire_id == wire_id and item.scene() is self.scene:
                item.setSelected(True)
                return

    @staticmethod
    def _pts_equal(a: list[Point], b: list[Point], tol: float = 1e-4) -> bool:
        if len(a) != len(b):
            return False
        return all(abs(p.x - q.x) <= tol and abs(p.y - q.y) <= tol for p, q in zip(a, b))

    def _wire_pts(self, wire_id: str) -> list[Point]:
        wire = self.session.find_wire(wire_id=wire_id)
        return list(
            iter_wire_points(
                self.session.sheet(), self.session.catalog, wire, project=self.session.project
            )
        )

    def _set_wire_item_path(self, item: WireItem, pts: list[Point]) -> None:
        path = QPainterPath()
        path.moveTo(pts[0].x, pts[0].y)
        for p in pts[1:]:
            path.lineTo(p.x, p.y)
        item.setPath(path)

    def _begin_wire_pull(self, item: WireItem, scene_pos: QPointF) -> bool:
        if not item.wire_id or is_layout_sheet(self.session.sheet()):
            return False
        try:
            pts = self._wire_pts(item.wire_id)
        except (KeyError, ValueError, IndexError):
            return False
        limit = max(4.0, wire_hit_radius_scene(self) + 0.5)
        hit = nearest_run(pts, scene_pos.x(), scene_pos.y(), max_dist=limit)
        if hit is None:
            return False
        self._begin_gesture("pull wire")
        self._pull_wire = item
        self._pull_pts = pts
        self._pull_seg, self._pull_kind = hit
        self._pull_moved = False
        item.setSelected(True)
        if self._pull_kind == "h":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        return True

    def _update_wire_pull(self, scene_pos: QPointF) -> None:
        if self._pull_wire is None or not self._pull_pts:
            return
        channel = snap(scene_pos.y() if self._pull_kind == "h" else scene_pos.x())
        pts = set_run_channel(self._pull_pts, self._pull_seg, self._pull_kind, channel)
        changed = not self._pts_equal(pts, self._pull_pts)
        self._pull_moved = changed
        if changed:
            self._clear_wire_handles()
        else:
            self._sync_wire_handles()
        self._set_wire_item_path(self._pull_wire, pts)
        self._pull_wire._live_pts = pts  # type: ignore[attr-defined]

    def _finalize_wire_route_drag(
        self,
        item: WireItem | None,
        pts: list[Point] | None,
        orig_pts: list[Point] | None,
        moved: bool,
        verb: str,
    ) -> None:
        self.unsetCursor()
        if item is None or not item.wire_id:
            return
        if not moved or not pts or len(pts) < 2:
            if orig_pts and len(orig_pts) >= 2:
                self._set_wire_item_path(item, orig_pts)
            self._sync_wire_handles()
            self.status.emit(self._status_text())
            return
        wire_id = item.wire_id
        self.session.set_wire_route(wire_id, pts[1:-1])
        self._redraw_wires()
        self._select_wire_item(wire_id)
        self.project_changed.emit()
        self.status.emit(f"{verb} wire {wire_id}  —  right-click to reset auto-route")

    def _commit_wire_pull(self) -> None:
        item = self._pull_wire
        pts = getattr(item, "_live_pts", None) if item is not None else None
        orig_pts = self._pull_pts
        self._pull_wire = None
        self._pull_pts = None
        self._pull_seg = -1
        self._pull_kind = ""
        moved = self._pull_moved
        self._pull_moved = False
        self._finalize_wire_route_drag(item, pts, orig_pts, moved, "pulled")

    def _begin_wire_corner_drag(self, handle: WireHandleItem) -> bool:
        if not handle.wire_id or is_layout_sheet(self.session.sheet()):
            return False
        wire_item = next(
            (i for i in self._items if isinstance(i, WireItem) and i.wire_id == handle.wire_id),
            None,
        )
        if wire_item is None:
            return False
        try:
            pts = self._wire_pts(handle.wire_id)
        except (KeyError, ValueError, IndexError):
            return False
        if handle.corner_index <= 0 or handle.corner_index >= len(pts) - 1:
            return False
        self._begin_gesture("pull wire")
        self._corner_wire = wire_item
        self._corner_pts = pts
        self._corner_index = handle.corner_index
        self._corner_moved = False
        wire_item.setSelected(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        return True

    def _update_wire_corner_drag(self, scene_pos: QPointF) -> None:
        if self._corner_wire is None or not self._corner_pts:
            return
        nx = snap(scene_pos.x())
        ny = snap(scene_pos.y())
        pts = move_wire_corner(self._corner_pts, self._corner_index, nx, ny)
        changed = not self._pts_equal(pts, self._corner_pts)
        self._corner_moved = changed
        if changed:
            self._clear_wire_handles()
        else:
            self._sync_wire_handles()
        self._set_wire_item_path(self._corner_wire, pts)
        self._corner_wire._live_pts = pts  # type: ignore[attr-defined]

    def _commit_wire_corner_drag(self) -> None:
        item = self._corner_wire
        pts = getattr(item, "_live_pts", None) if item is not None else None
        orig_pts = self._corner_pts
        self._corner_wire = None
        self._corner_pts = None
        self._corner_index = -1
        moved = self._corner_moved
        self._corner_moved = False
        self._finalize_wire_route_drag(item, pts, orig_pts, moved, "rerouted")

    def _clear_wire_end_visuals(self) -> None:
        drag = self._wire_end_drag
        if isinstance(drag, dict):
            hot_pin = drag.get("hot_pin")
            if hot_pin is not None:
                try:
                    if hot_pin.scene() is not None:
                        hot_pin._hot = False
                        hot_pin.update()
                except RuntimeError:
                    pass
                drag["hot_pin"] = None
        for attr in ("_wire_end_preview", "_wire_end_snap"):
            item = getattr(self, attr, None)
            if item is None:
                continue
            if item in self._items:
                self._items.remove(item)
            try:
                if item.scene() is not None and self.scene is not None:
                    self.scene.removeItem(item)
            except RuntimeError:
                pass
            setattr(self, attr, None)

    def _cancel_wire_end_drag(self, *, resync: bool = True) -> None:
        drag = self._wire_end_drag
        self._wire_end_drag = None
        self._clear_wire_end_visuals()
        self.unsetCursor()
        if resync and drag is not None:
            self._sync_wire_handles()
            self.status.emit(self._status_text())

    def _validate_wire_end_drop(
        self, wire, which: str, pin: PinItem | None, rail
    ) -> tuple[str | None, QPointF | None, str]:
        if pin is not None:
            target_ref = pin.ref()
            snap_pt = pin.scenePos()
        elif rail is not None:
            target_ref = str(rail.name)
            snap_pt = QPointF(float(rail.x), float(rail.y))
        else:
            return None, None, "drop onto a valid pin or rail"

        new_from = target_ref if which == "from" else wire.frm
        new_to = target_ref if which == "to" else wire.to
        try:
            self.session.validate_wire_endpoints(self.session.sheet(), new_from, new_to)
        except (KeyError, ValueError) as exc:
            return None, snap_pt, str(exc)
        return target_ref, snap_pt, ""

    @staticmethod
    def _end_drag_moved(drag: dict, view_pos: QPointF, scene_pos: QPointF) -> bool:
        if drag["moved"]:
            return True
        if (view_pos - drag["press_view_pos"]).manhattanLength() >= 4.0:
            return True
        return (
            math.hypot(
                scene_pos.x() - drag["start_pos"].x(),
                scene_pos.y() - drag["start_pos"].y(),
            )
            >= 1.0
        )

    def _begin_wire_end_drag(
        self, handle: WireHandleItem, view_pos: QPointF, scene_pos: QPointF
    ) -> bool:
        if not handle.wire_id or handle.which not in ("from", "to"):
            return False
        if is_layout_sheet(self.session.sheet()):
            return False
        try:
            pts = self._wire_pts(handle.wire_id)
        except (KeyError, ValueError, IndexError):
            return False
        if len(pts) < 2:
            return False
        anchor_pt = pts[-1] if handle.which == "from" else pts[0]
        start_pt = pts[0] if handle.which == "from" else pts[-1]
        self._wire_end_drag = {
            "wire_id": handle.wire_id,
            "which": handle.which,
            "anchor": QPointF(anchor_pt.x, anchor_pt.y),
            "start_pos": QPointF(start_pt.x, start_pt.y),
            "press_view_pos": QPointF(view_pos),
            "moved": False,
            "target_ref": None,
            "snap_at": None,
            "snap_is_rail": False,
            "invalid_msg": "",
            "hot_pin": None,
        }
        self.setCursor(Qt.CursorShape.CrossCursor)
        return True

    def _update_wire_end_drag(self, view_pos: QPointF, scene_pos: QPointF) -> None:
        drag = self._wire_end_drag
        if drag is None or self.scene is None:
            return
        if not drag["moved"]:
            if not self._end_drag_moved(drag, view_pos, scene_pos):
                return
            drag["moved"] = True
            self._clear_wire_handles()

        try:
            wire = self.session.find_wire(wire_id=drag["wire_id"])
        except KeyError:
            self._cancel_wire_end_drag()
            return

        pin = self._pin_at(view_pos)
        rail = None if pin is not None else self._rail_at(scene_pos)
        target_ref, snap_pt, err = self._validate_wire_end_drop(wire, drag["which"], pin, rail)

        old_hot = drag.get("hot_pin")
        new_hot = pin if target_ref is not None and pin is not None else None
        if old_hot is not None and old_hot is not new_hot:
            try:
                if old_hot.scene() is not None:
                    old_hot._hot = False
                    old_hot.update()
            except RuntimeError:
                pass
        if new_hot is not None:
            new_hot._hot = True
            new_hot.update()
        drag["hot_pin"] = new_hot

        drag["target_ref"] = target_ref
        drag["snap_at"] = snap_pt
        drag["snap_is_rail"] = bool(target_ref is not None and pin is None and rail is not None)
        drag["invalid_msg"] = err

        anchor: QPointF = drag["anchor"]
        end_pt = snap_pt if snap_pt is not None else scene_pos
        path = QPainterPath()
        path.moveTo(anchor.x(), anchor.y())
        mid = (anchor.x() + end_pt.x()) / 2.0
        path.lineTo(mid, anchor.y())
        path.lineTo(mid, end_pt.y())
        path.lineTo(end_pt.x(), end_pt.y())
        invalid = target_ref is None and snap_pt is not None
        pen_color = _qc("finding_error") if invalid else _qc("wire_preview")
        if self._wire_end_preview is None:
            preview = QGraphicsPathItem()
            preview.setZValue(31)
            preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            preview.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._add(preview)
            self._wire_end_preview = preview
        self._wire_end_preview.setPen(QPen(pen_color, 0.65, Qt.PenStyle.DashLine))
        self._wire_end_preview.setPath(path)

        if target_ref is not None and snap_pt is not None:
            if self._wire_end_snap is None:
                ring = QGraphicsEllipseItem(-3.2, -3.2, 6.4, 6.4)
                ring.setZValue(33)
                ring.setPen(QPen(_qc("highlight"), 0.7))
                ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                ring.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                ring.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                self._add(ring)
                self._wire_end_snap = ring
            self._wire_end_snap.setPos(snap_pt)
            self._wire_end_snap.setVisible(True)
            self.status.emit(f"reconnect {wire.id} {drag['which']} → {target_ref}")
        else:
            if self._wire_end_snap is not None:
                self._wire_end_snap.setVisible(False)
            self.status.emit(err or "drop onto a valid pin or rail (Esc cancels)")

    def _commit_wire_end_drag(self, view_pos: QPointF, scene_pos: QPointF) -> None:
        drag = self._wire_end_drag
        if drag is None:
            return
        if not self._end_drag_moved(drag, view_pos, scene_pos):
            self._cancel_wire_end_drag()
            return
        self._update_wire_end_drag(view_pos, scene_pos)
        drag = self._wire_end_drag
        if drag is None:
            return
        wire_id = str(drag["wire_id"])
        which = str(drag["which"])
        target_ref = drag.get("target_ref")
        snap_at = drag.get("snap_at")
        snap_is_rail = bool(drag.get("snap_is_rail"))
        invalid_msg = str(drag.get("invalid_msg") or "")

        self._wire_end_drag = None
        self._clear_wire_end_visuals()
        self.unsetCursor()

        if not target_ref:
            self._sync_wire_handles()
            self.status.emit(invalid_msg or "drop refused — not a valid pin or rail")
            return
        try:
            wire = self.session.find_wire(wire_id=wire_id)
        except KeyError:
            self._sync_wire_handles()
            return
        current_ref = wire.frm if which == "from" else wire.to
        if self._ends_match(target_ref, current_ref) and not snap_is_rail:
            self._sync_wire_handles()
            self.status.emit(self._status_text())
            return

        kwargs: dict = {}
        if snap_is_rail and snap_at is not None:
            kwargs["from_at" if which == "from" else "to_at"] = (
                float(snap_at.x()),
                float(snap_at.y()),
            )
        try:
            self.session.reconnect_wire(wire_id, which, target_ref, **kwargs)
        except (KeyError, ValueError) as exc:
            self._sync_wire_handles()
            self.status.emit(str(exc))
            return
        self._redraw_wires()
        self._select_wire_item(wire_id)
        self.project_changed.emit()
        self.status.emit(f"reconnected {wire_id} ({which}) → {target_ref}")

    def _in_title_block(self, scene_pos: QPointF) -> bool:
        x, y, w, h = title_block_rect(self.session.sheet())
        return QRectF(x, y, w, h).contains(scene_pos)

    def _begin_empty_select_drag(self, event) -> None:
        """Hold-drag on empty paper: leave place/wire and rubber-band select."""
        self.set_select_mode()
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        origin = self._left_press_pos if self._left_press_pos is not None else event.position()
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            event.modifiers(),
        )
        super().mousePressEvent(press)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._moved_since_press = False
        self._dragged_ids.clear()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self._pan_start = event.position()
            self._middle_press_pos = event.position()
            self._middle_hit_device = None
            self._middle_hit_wire = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if not self._in_title_block(scene_pos):
                pin = None if is_layout_sheet(self.session.sheet()) else self._pin_at(event.position())
                wire = None if pin is not None else self._wire_at(event.position())
                device = None if pin is not None or wire is not None else self._device_at(event.position())
                if pin is not None:
                    parent = pin.parentItem()
                    device = parent if isinstance(parent, ComponentItem) else self._device_at(event.position())
                    wire = None
                self._middle_hit_device = device
                self._middle_hit_wire = wire
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            # Do not start a drag — contextMenuEvent builds the command menu.
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        self._left_press_pos = event.position()
        self._left_empty_press = False
        self._left_select_drag = False

        hit = self.itemAt(event.position().toPoint())
        if isinstance(hit, LogoSlotItem):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        if self._in_title_block(scene_pos):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            event.accept()
            return

        if self.mode == "cloud":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._cloud_origin = scene_pos
            self._update_cloud_preview(scene_pos, scene_pos)
            event.accept()
            return
        if self.mode == "zip_tie":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._zip_tie_origin = scene_pos
            self._update_zip_tie_preview(scene_pos, scene_pos)
            event.accept()
            return

        layout_sheet = is_layout_sheet(self.session.sheet())
        if self._wire_end_drag is not None:
            self._cancel_wire_end_drag()
        handle = (
            None
            if layout_sheet or self.wire_from is not None or self.mode in ("place", "cloud", "zip_tie")
            else self._wire_handle_at(event.position(), scene_pos)
        )
        if handle is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            if handle.kind == "end" and self._begin_wire_end_drag(handle, event.position(), scene_pos):
                event.accept()
                return
            if handle.kind == "corner" and self._begin_wire_corner_drag(handle):
                event.accept()
                return

        pin = None if layout_sheet else self._pin_at(event.position())
        # Rails are wire ends only while the Wire tool is on, or a wire is
        # already started. Select and Place keep the click they had before.
        wiring = self.mode == "wire" or self.wire_from is not None
        rail = self._rail_at(scene_pos) if wiring and pin is None else None
        # A wire on top of a device's padded bounding box is still a wire click.
        wire = None if pin is not None or rail is not None else self._wire_at(event.position())
        device = None if pin is not None or rail is not None or wire is not None else self._device_at(event.position())

        if pin is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            locked = True
            try:
                locked = self.session.sheet().component(pin.component_id).pins_locked
            except KeyError:
                locked = True
            if (not locked) and self.wire_from is None:
                self._begin_gesture("slide pin")
                self._slide_pin = pin
                self._slide_moved = False
                event.accept()
                return
            self._wire_drag = True
            self._handle_pin(pin.ref())
            self._update_wire_preview(scene_pos, None)
            event.accept()
            return

        if rail is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._wire_drag = True
            self._handle_wire_end(rail.name, QPointF(rail.x, rail.y))
            self._update_wire_preview(scene_pos, None)
            event.accept()
            return

        # Existing device: always select/drag. Never place-on-top, never clear the scene.
        if device is not None:
            self._begin_gesture("move")
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            super().mousePressEvent(event)
            return

        if wire is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            if not wire.isSelected() or len(self.scene.selectedItems()) != 1:
                self.scene.clearSelection()
                wire.setSelected(True)
            if self._begin_wire_pull(wire, scene_pos):
                cap = f"  [{wire._label}]" if wire._label else ""
                self.status.emit(f"wire {wire.wire_id}{cap}  —  drag to pull, right-click to label or reset")
            else:
                cap = f"  [{wire._label}]" if wire._label else ""
                self.status.emit(f"wire {wire.wire_id}{cap}  —  right-click to label or delete")
            event.accept()
            return

        # Empty workspace: short click keeps the current tool; hold-drag enters select.
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._left_empty_press = True
        event.accept()
        return

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not event.buttons():
            self._set_wire_hover(self._hover_wire_at(event.position()))
        if event.buttons() & Qt.MouseButton.MiddleButton:
            if self._middle_press_pos is not None and not self._panning:
                if (event.position() - self._middle_press_pos).manhattanLength() >= 8:
                    self._panning = True
                    self._pan_start = event.position()
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._panning:
                delta = event.position() - self._pan_start
                self._pan_start = event.position()
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self._cloud_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            self._update_cloud_preview(self._cloud_origin, end)
            event.accept()
            return
        if getattr(self, "_zip_tie_origin", None) is not None and event.buttons() & Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            self._update_zip_tie_preview(self._zip_tie_origin, end)
            event.accept()
            return
        if self._slide_pin is not None:
            self._slide_pin_to(event)
            event.accept()
            return
        if self._wire_end_drag is not None:
            self._update_wire_end_drag(event.position(), self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._corner_wire is not None:
            self._update_wire_corner_drag(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._pull_wire is not None:
            self._update_wire_pull(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._left_press_pos is not None
            and not self._wire_drag
        ):
            if not self._left_select_drag:
                if (event.position() - self._left_press_pos).manhattanLength() >= 8:
                    self._left_select_drag = True
                    if self._left_empty_press:
                        self._begin_empty_select_drag(event)
                        event.accept()
                        return
                    self.set_select_mode()
            elif self._left_empty_press:
                super().mouseMoveEvent(event)
                event.accept()
                return
        if self.wire_from:
            scene_pos = self.mapToScene(event.position().toPoint())
            pin = self._pin_at(event.position())
            if pin is not None and self._ends_match(pin.ref(), self.wire_from):
                pin = None
            rail = None if pin is not None else self._rail_at(scene_pos)
            snap_at = None
            if rail is not None and not self._ends_match(rail.name, self.wire_from):
                snap_at = QPointF(rail.x, rail.y)
            self._update_wire_preview(scene_pos, pin, snap_at)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._gesture_edit
            and not self.session.command_open()
        ):
            # Undo or Redo already cancelled this drag. Do not push it back.
            self._drop_gesture_flags()
            super().mouseReleaseEvent(event)
            self._left_press_pos = None
            self._left_select_drag = False
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._resync_items_from_model()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            panned = self._panning
            self._panning = False
            self.unsetCursor()
            if not panned:
                self._delete_middle_hit()
            self._middle_press_pos = None
            self._middle_hit_device = None
            self._middle_hit_wire = None
            event.accept()
            return
        if getattr(self, "_zip_tie_origin", None) is not None and event.button() == Qt.MouseButton.LeftButton:
            origin = self._zip_tie_origin
            end = self.mapToScene(event.position().toPoint())
            self._cancel_zip_tie_drag()
            dist = ((end.x() - origin.x()) ** 2 + (end.y() - origin.y()) ** 2) ** 0.5
            if dist >= 2.0:
                self.commit_zip_tie_slice(origin.x(), origin.y(), end.x(), end.y())
            else:
                self.status.emit("drag across routed wires (at least 2 mm) to bundle with a zip-tie")
            event.accept()
            return
        if self._cloud_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            origin = self._cloud_origin
            end = self.mapToScene(event.position().toPoint())
            self._cancel_cloud_drag()
            x = min(origin.x(), end.x())
            y = min(origin.y(), end.y())
            w = abs(end.x() - origin.x())
            h = abs(end.y() - origin.y())
            if w >= 3.0 and h >= 3.0:
                try:
                    self.commit_cloud(x, y, w, h)
                except ValueError as exc:
                    self.status.emit(str(exc))
            else:
                self.status.emit("drag a revision cloud at least 3 mm")
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._left_empty_press:
            dragged = self._left_select_drag
            press_pos = self._left_press_pos or event.position()
            self._left_empty_press = False
            self._left_select_drag = False
            self._left_press_pos = None
            if dragged:
                super().mouseReleaseEvent(event)
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
                event.accept()
                return
            if self.mode == "place" and self.place_library_id:
                scene_pos = self.mapToScene(press_pos.toPoint())
                self.place_at(self.place_library_id, scene_pos.x(), scene_pos.y())
                event.accept()
                return
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            press = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                press_pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mousePressEvent(press)
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            event.accept()
            return
        if self._wire_end_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            self._commit_wire_end_drag(event.position(), self.mapToScene(event.position().toPoint()))
            self._left_press_pos = None
            self._left_select_drag = False
            event.accept()
            return
        if self._corner_wire is not None and event.button() == Qt.MouseButton.LeftButton:
            moved = self._corner_moved
            self._commit_wire_corner_drag()
            self._finish_gesture(moved=moved)
            self._left_press_pos = None
            self._left_select_drag = False
            event.accept()
            return
        if self._pull_wire is not None and event.button() == Qt.MouseButton.LeftButton:
            moved = self._pull_moved
            self._commit_wire_pull()
            self._finish_gesture(moved=moved)
            self._left_press_pos = None
            self._left_select_drag = False
            event.accept()
            return
        if self._slide_pin is not None and event.button() == Qt.MouseButton.LeftButton:
            pin = self._slide_pin
            moved = self._slide_moved
            self._slide_pin = None
            self._slide_moved = False
            if moved:
                self._commit_slide(pin)
                self._finish_gesture(moved=True)
            else:
                self._finish_gesture(moved=False)
                self._wire_drag = True
                self._handle_pin(pin.ref())
                self._update_wire_preview(self.mapToScene(event.position().toPoint()), None)
            event.accept()
            return
        if self._wire_drag and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            pin = self._pin_at(event.position())
            if pin is not None and self.wire_from and not self._ends_match(pin.ref(), self.wire_from):
                self._handle_pin(pin.ref())
            else:
                rail = None if pin is not None else self._rail_at(scene_pos)
                if rail is not None and self.wire_from and not self._ends_match(rail.name, self.wire_from):
                    self._handle_wire_end(rail.name, QPointF(rail.x, rail.y))
            self._wire_drag = False
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._left_press_pos = None
        self._left_select_drag = False
        user_moved = bool(self._moved_since_press and self._dragged_ids)
        moved = self._commit_symbol_motion(lift=True)
        self._dragged_ids.clear()
        self._finish_gesture(moved=bool(moved or user_moved))
        self._moved_since_press = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # A cached scene can still be mid-drag when the button comes up.
        # The commit wrote the model; the picture has to follow it.
        self._resync_items_from_model()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._clear_wire_hover()
        super().leaveEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if _library_id_from_mime(event.mimeData()) or _bom_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if _library_id_from_mime(event.mimeData()) or _bom_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        scene_pos = self.mapToScene(event.position().toPoint())
        bom = _bom_from_mime(event.mimeData())
        if bom and bom.get("library_id"):
            self.place_from_bom_at(
                str(bom["library_id"]),
                str(bom.get("ref") or "") or None,
                scene_pos.x(),
                scene_pos.y(),
            )
            event.acceptProposedAction()
            return
        lib_id = _library_id_from_mime(event.mimeData())
        if not lib_id:
            event.ignore()
            return
        self.set_place_mode(lib_id)
        self.place_at(lib_id, scene_pos.x(), scene_pos.y())
        event.acceptProposedAction()

    def _slide_pin_to(self, event) -> None:
        pin = self._slide_pin
        if pin is None:
            return
        parent = pin.parentItem()
        if not isinstance(parent, ComponentItem):
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        local = parent.mapFromScene(scene_pos)
        scale = max(parent._scale, 0.01)
        x = round((local.x() / scale) * 2.0) / 2.0
        y = round((local.y() / scale) * 2.0) / 2.0
        x = max(-4.0, min(parent._base_w + 4.0, x))
        y = max(-4.0, min(parent._base_h + 4.0, y))
        new_pos = QPointF(x * scale, y * scale)
        if (new_pos - pin.pos()).manhattanLength() > 0.6:
            self._slide_moved = True
        pin._lx = x
        pin._ly = y
        pin.setPos(new_pos)
        try:
            comp = self.session.sheet().component(pin.component_id)
        except KeyError:
            return
        side = infer_pin_side(x, y, parent._base_w, parent._base_h)
        comp.pin_overrides[pin.pin_id] = PinOverride(x=x, y=y, side=side)  # type: ignore[arg-type]
        self._redraw_wires()

    def _commit_slide(self, pin: PinItem) -> None:
        try:
            ov = self.session.set_pin(pin.component_id, pin.pin_id, pin._lx, pin._ly)
        except (KeyError, ValueError) as exc:
            self.status.emit(str(exc))
            return
        self.session.drop_wire_routes({pin.component_id})
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"{pin.ref()}  {ov.x:g},{ov.y:g}  {ov.side}  — lock the device when finished")

    def set_selected_pins_locked(self, locked: bool) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not refs:
            self.status.emit("select a device first, then lock or unlock its connection points")
            return
        last = None
        self.session.begin_edit("lock pins" if locked else "unlock pins")
        self.sync_positions()
        try:
            for ref in refs:
                last = self.session.set_pins_locked(ref, locked)
        finally:
            self.session.end_edit()
        self._apply_pin_mode()
        word = "locked" if locked else "unlocked — drag a terminal onto the connection point"
        self.status.emit(f"{last.id if last else ''} connection points {word}")
        self.project_changed.emit()

    def reset_selected_pins(self) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not refs:
            self.status.emit("select a device first to reset its connection points")
            return
        self.session.begin_edit("reset pins")
        self.sync_positions()
        try:
            for ref in refs:
                self.session.reset_pins(ref)
        finally:
            self.session.end_edit()
        self.reload()
        if refs:
            self.select_ref(refs[-1])
        self.status.emit("connection points restored to library positions")

    def _rail_at(self, scene_pos: QPointF):
        if is_layout_sheet(self.session.sheet()) or self.session.sheet().kind == "terminal":
            return None
        from panelcad.core.ladder import hit_rail

        return hit_rail(
            self.session.sheet(), scene_pos.x(), scene_pos.y(), self.session.project
        )

    def _ends_match(self, left: str, right: str) -> bool:
        return str(left).strip().upper() == str(right).strip().upper()

    def _handle_pin(self, ref: str) -> None:
        self._handle_wire_end(ref, None)

    def _handle_wire_end(self, ref: str, at: QPointF | None) -> None:
        if self.wire_from is None:
            self.wire_from = ref
            self._wire_from_at = QPointF(at) if at is not None else None
            self.status.emit(f"wire from {ref} — click the other pin or rail (Esc cancels)")
            return
        if self._ends_match(self.wire_from, ref):
            self.wire_from = None
            self._wire_from_at = None
            self._wire_drag = False
            self._clear_wire_preview()
            self.status.emit("wire cancelled")
            return
        from panelcad.core.ladder import rail_short_message

        short = rail_short_message(
            self.session.sheet(), self.wire_from, ref, self.session.project
        )
        if short:
            self.status.emit(short)
            return
        kwargs: dict = {}
        if self._wire_from_at is not None:
            kwargs["from_at"] = (self._wire_from_at.x(), self._wire_from_at.y())
        if at is not None:
            kwargs["to_at"] = (at.x(), at.y())
        self.session.begin_edit("wire")
        self.sync_positions()
        try:
            self.session.wire(self.wire_from, ref, **kwargs)
        finally:
            self.session.end_edit()
        self.wire_from = None
        self._wire_from_at = None
        self._wire_drag = False
        self._clear_wire_preview()
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(self._status_text())

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom_by(factor)

    def zoom_by(self, factor: float) -> None:
        self._user_zoomed = True
        self.scale(factor, factor)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def zoom_percent(self) -> int:
        """100% is 1 mm on the sheet equals 1 mm on the screen."""

        dpi = float(self.logicalDpiX() or 96)
        true = float(self.transform().m11()) * (25.4 / dpi)
        return max(1, int(round(true * 100)))

    def paintEvent(self, event) -> None:  # noqa: N802
        from panelcad.theme import canvas_hex, push_screen_ink, reset_screen_ink

        self._problems_marks = _problems_tab_current(self)
        token = push_screen_ink(canvas_hex("ink"))
        try:
            super().paintEvent(event)
        finally:
            reset_screen_ink(token)

    def set_place_mode(self, library_id: str) -> None:
        self.mode = "place"
        self.place_library_id = library_id
        self.wire_from = None
        self._wire_from_at = None
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit(f"place {library_id} — drag from library or click empty sheet (not on a part)")

    def _draw_terminal_strip(self, sheet) -> None:
        from panelcad.gui.strip_canvas import StripBlockItem, StripJumperItem, StripSceneItem
        from panelcad.render.terminal_strip import physical_scene_for_sheet, terminal_strip_caption

        scene = physical_scene_for_sheet(self.session.project, self.session.catalog, sheet)
        self._add(StripSceneItem(scene))
        for block in scene.get("blocks") or []:
            self._add(StripBlockItem(block))
        for jumper in scene.get("jumpers") or []:
            self._add(StripJumperItem(jumper))
        name = str(sheet.strip or "").strip()
        if not name:
            names = [str(group.get("strip") or "") for group in scene.get("groups") or []]
            name = ", ".join(names) if names else ""
        n = sum(1 for block in scene.get("blocks") or [] if block.get("kind") == "terminal")
        if not name:
            self._strip_caption = "no strip assigned — Insert → Terminal strip…"
            return
        page = int(scene.get("page_index") or getattr(sheet, "strip_part", 1) or 1)
        pages = int(scene.get("page_count") or 1)
        self._strip_caption = terminal_strip_caption(name, n, page, pages)

    def select_ref(self, ref: str) -> None:
        self.scene.clearSelection()
        for item in self.scene.items():
            if getattr(item, "component_id", None) != ref:
                continue
            if isinstance(item, ComponentItem) or getattr(item, "strip_block", False):
                item.setSelected(True)
                self.centerOn(item)
                return
        for item in self.scene.items():
            if isinstance(item, WireItem) and item.wire_id == ref:
                item.setSelected(True)
                self.centerOn(item)
                return

    def set_findings(self, rows: list[dict[str, str]]) -> None:
        self._findings = [row for row in rows if row.get("suppressed") != "yes"]
        self._draw_finding_markers()

    def _draw_finding_markers(self) -> None:
        for item in list(self._items):
            if isinstance(item, FindingMarker):
                self.scene.removeItem(item)
                self._items.remove(item)
        sheet_id = self.session.sheet().id
        visible = [row for row in self._findings if str(row.get("sheet") or "") in {"", sheet_id}]
        components = {item.component_id: item for item in self._items if isinstance(item, ComponentItem)}
        wires = {item.wire_id: item for item in self._items if isinstance(item, WireItem) and item.wire_id}
        hosts: dict[str, str] = {}
        for wire in self.session.sheet().wires:
            ends = [_host_component(str(end), set(components)) for end in (wire.frm, wire.to)]
            live = [end for end in ends if end]
            if len(live) == 1:
                hosts[wire.id] = live[0]
        obstacles = [device_drawn_scene_bounds(item) for item in components.values()]
        for kind, ident, rows in group_finding_rows(visible, set(components), set(wires), hosts=hosts):
            worst = rows[0]
            marker = FindingMarker(worst, count=len(rows), rows=rows, anchor_ref=ident)
            br = marker.boundingRect()
            tip: QPointF | None = None
            if kind == "comp" and ident in components:
                box = device_drawn_scene_bounds(components[ident])
                pos = marker_pos_off_top_right(box, br)
                tip = QPointF(box.right(), box.top())
            elif kind == "wire" and ident in wires:
                box = wire_drawn_scene_bounds(wires[ident])
                pos = marker_pos_off_top_right(box, br)
                tip = QPointF(box.right(), box.top())
            else:
                try:
                    raw = QPointF(float(worst.get("x") or 0), float(worst.get("y") or 0))
                except (TypeError, ValueError):
                    continue
                box = QRectF(raw.x() - 1.0, raw.y() - 1.0, 2.0, 2.0)
                pos = marker_pos_off_top_right(box, br)
                tip = raw
            pos = place_clear_of_devices(pos, br, obstacles)
            marker.setPos(pos)
            self._add(marker)
            if tip is not None:
                marker.set_leader(tip)

    def highlight_trace(
        self,
        devices: list[str],
        wires: list[str],
        *,
        focus: str = "",
        focus_wire: str = "",
        sheet_id: str = "",
    ) -> None:
        """Mark the troubleshooting path. A reload keeps the same marks."""

        self._trace_highlight = {
            "devices": [str(item) for item in devices if item],
            "wires": [str(item) for item in wires if item],
            "focus": str(focus or ""),
            "focus_wire": str(focus_wire or ""),
            "sheet": str(sheet_id or ""),
        }
        self._draw_trace_highlight()
        if focus:
            self.select_ref(focus)

    def clear_trace_highlight(self) -> None:
        self._trace_highlight = None
        self._remove_trace_marks()

    def _remove_trace_marks(self) -> None:
        for item in list(self._items):
            if isinstance(item, (TraceHalo, TraceWireMark)):
                if item.scene() is not None:
                    self.scene.removeItem(item)
                self._items.remove(item)

    def _draw_trace_highlight(self) -> None:
        self._remove_trace_marks()
        spec = self._trace_highlight
        if not spec:
            return
        sheet = self.session.sheet()
        wanted_sheet = str(spec.get("sheet") or "")
        if wanted_sheet and wanted_sheet != sheet.id:
            return
        devices = {name.casefold() for name in spec.get("devices") or []}
        focus = str(spec.get("focus") or "").casefold()
        numbers = {str(item) for item in spec.get("wires") or [] if item}
        focus_wire = str(spec.get("focus_wire") or "")
        by_id = {wire.id: str(wire.number or "").strip() for wire in sheet.wires}
        for item in list(self._items):
            if isinstance(item, ComponentItem) and item.component_id.casefold() in devices:
                rect = device_drawn_scene_bounds(item)
                if rect.isValid() and rect.width() > 0 and rect.height() > 0:
                    self._add(TraceHalo(rect, focus=item.component_id.casefold() == focus))
            elif isinstance(item, WireItem) and item.wire_id:
                number = by_id.get(item.wire_id, "")
                if number and number in numbers:
                    self._add(TraceWireMark(item.path(), focus=number == focus_wire))

    def jump_to_finding(self, row: dict[str, str]) -> None:
        ref = str(row.get("ref") or "")
        if ref:
            self.select_ref(ref)
            if not self.scene.selectedItems() and str(row.get("target") or ""):
                self.select_ref(str(row["target"]))
        try:
            x = float(row.get("x") or "")
            y = float(row.get("y") or "")
        except (TypeError, ValueError):
            return
        self.centerOn(x, y)

    def fit_page(self, *, user: bool = True) -> None:
        """Show the whole sheet, including the bottom title block."""
        self._user_zoomed = user
        self.resetTransform()
        rect = self.scene.sceneRect()
        if rect.isEmpty():
            return
        self.fitInView(rect.adjusted(-4, -4, 4, 4), Qt.AspectRatioMode.KeepAspectRatio)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def fit_drawing(self) -> None:
        """Fit the devices and wires, not the empty page around them."""

        rect = QRectF()
        for item in self.scene.items():
            if isinstance(item, (ComponentItem, WireItem, EnclosureItem, TrayItem, RailItem)):
                rect = rect.united(item.sceneBoundingRect())
        if rect.isEmpty() or rect.width() < 1 or rect.height() < 1:
            self.fit_page(user=False)
            return
        self._user_zoomed = False
        self.resetTransform()
        self.fitInView(rect.adjusted(-8, -8, 8, 8), Qt.AspectRatioMode.KeepAspectRatio)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def _fit_if_automatic(self) -> None:
        if self._user_zoomed:
            return
        self.fit_drawing()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "_user_zoomed", True):
            return
        timer = getattr(self, "_fit_timer", None)
        if timer is not None:
            timer.start(150)

    def selected_refs(self) -> list[str]:
        refs: list[str] = []
        for item in self.scene.selectedItems():
            if isinstance(item, ComponentItem):
                refs.append(item.component_id)
            elif isinstance(item, WireItem) and item.wire_id:
                refs.append(item.wire_id)
        return refs

    def restore_selection(self, refs: list[str]) -> None:
        wanted = {ref for ref in refs if ref}
        if not wanted:
            return
        self.scene.blockSignals(True)
        try:
            for item in self.scene.items():
                if isinstance(item, ComponentItem) and item.component_id in wanted:
                    item.setSelected(True)
                elif isinstance(item, WireItem) and item.wire_id in wanted:
                    item.setSelected(True)
        finally:
            self.scene.blockSignals(False)
        self.selection_changed.emit()

    def set_select_mode(self) -> None:
        self.mode = "select"
        self.place_library_id = None
        self.cancel_wire()
        self._cancel_cloud_drag()
        self._cancel_zip_tie_drag()
        self._apply_pin_mode()
        self.unsetCursor()
        self.status.emit(self._status_text())

    @property
    def zip_tie_mode(self) -> bool:
        return self.mode == "zip_tie"

    @property
    def _zip_tie_mode(self) -> bool:
        return self.mode == "zip_tie"

    def set_zip_tie_mode(self, enabled: bool = True) -> None:
        if not enabled:
            self._cancel_zip_tie_drag()
            if self.mode == "zip_tie":
                self.set_select_mode()
            return
        self.mode = "zip_tie"
        self.place_library_id = None
        self.cancel_wire()
        self._cancel_cloud_drag()
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit("ZIP-TIE SLICE — drag a line across panel wires to bundle them into a zip-tie waypoint")

    def commit_zip_tie_slice(self, x1: float, y1: float, x2: float, y2: float) -> object:
        tie = self.session.add_zip_tie_slice(float(x1), float(y1), float(x2), float(y2))
        self.reload()
        self.status.emit(f"zip-tie {tie.id} bundled {len(tie.wire_numbers)} wire(s)")
        return tie

    def _cancel_zip_tie_drag(self) -> None:
        self._zip_tie_origin = None
        item = getattr(self, "_zip_tie_preview", None)
        self._zip_tie_preview = None
        if item is None:
            return
        if item.scene() is not None:
            self.scene.removeItem(item)
        try:
            self._items.remove(item)
        except ValueError:
            pass

    def _update_zip_tie_preview(self, origin: QPointF, end: QPointF) -> None:
        path = QPainterPath()
        path.moveTo(origin.x(), origin.y())
        path.lineTo(end.x(), end.y())
        pen = QPen(_qc("wire_preview"), 0.8, Qt.PenStyle.DashLine)
        if getattr(self, "_zip_tie_preview", None) is None:
            item = QGraphicsPathItem()
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(75)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._add(item)
            self._zip_tie_preview = item
        self._zip_tie_preview.setPen(pen)
        self._zip_tie_preview.setPath(path)

    def set_panel_wiring_mode(self, mode: str) -> dict:
        res = self.session.set_panel_wiring_mode(mode)
        self.reload()
        return res

    def set_walkthrough_step(self, step_index: int) -> dict:
        res = self.session.set_walkthrough_step(step_index)
        self.reload()
        return res

    def step_walkthrough(self, delta: int = 1) -> dict:
        res = self.session.step_walkthrough(delta)
        self.reload()
        return res

    def set_cloud_mode(self) -> None:
        self.mode = "cloud"
        self.place_library_id = None
        self.cancel_wire()
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit("drag a rectangle to draw a revision cloud")

    def commit_cloud(self, x: float, y: float, w: float, h: float, rev: str = "") -> dict:
        """Store a manual revision cloud on the active sheet and redraw."""

        info = self.session.add_revision_cloud(float(x), float(y), float(w), float(h), rev=rev)
        self.reload()
        self.status.emit(f"revision cloud {info.get('id', '')}")
        return info

    def _draw_revision_clouds(self) -> None:
        store = self.session.project.revision_control
        if store is None:
            return
        sheet = self.session.sheet()
        for cloud in store.clouds:
            if cloud.sheet == sheet.id and cloud.w > 0 and cloud.h > 0:
                self._add(RevisionCloudItem(cloud, sheet, self.session.catalog))

    def _cancel_cloud_drag(self) -> None:
        self._cloud_origin = None
        item = self._cloud_preview
        self._cloud_preview = None
        if item is None:
            return
        if item.scene() is not None:
            self.scene.removeItem(item)
        try:
            self._items.remove(item)
        except ValueError:
            pass

    def _update_cloud_preview(self, origin: QPointF, end: QPointF) -> None:
        from panelcad.render.clouds import cloud_polyline

        x = min(origin.x(), end.x())
        y = min(origin.y(), end.y())
        w = max(abs(end.x() - origin.x()), 0.4)
        h = max(abs(end.y() - origin.y()), 0.4)
        pts = cloud_polyline(x, y, w, h)
        path = QPainterPath()
        path.moveTo(pts[0][0], pts[0][1])
        for px, py in pts[1:]:
            path.lineTo(px, py)
        from panelcad.render.clouds import CLOUD
        from panelcad.theme import adapt_print_color

        pen = QPen(QColor(adapt_print_color(CLOUD)), 0.6, Qt.PenStyle.DashLine)
        if self._cloud_preview is None:
            item = QGraphicsPathItem()
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(66)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._add(item)
            self._cloud_preview = item
        self._cloud_preview.setPen(pen)
        self._cloud_preview.setPath(path)

    def set_wire_mode(self) -> None:
        if is_layout_sheet(self.session.sheet()):
            self.set_select_mode()
            self.status.emit("wires are schematic-only — use a schematic or comms tab")
            return
        self.mode = "wire"
        self.place_library_id = None
        if self.scene is not None:
            self.scene.clearSelection()
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit("hover a terminal or a rail, then click or drag to the next one")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._left_empty_press = False
            self._left_select_drag = False
            self._left_press_pos = None
            self.cancel_wire()
            # Escape also cancels a half-finished move so the lock cannot stick.
            if self._gesture_edit or self.session.command_open():
                self.drop_open_gesture()
                self._resync_items_from_model()
                self._redraw_wires()
            self.set_select_mode()
            event.accept()
            return
        step = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }.get(event.key())
        if step is not None and self._nudge_selected(step[0], step[1]):
            event.accept()
            return
        super().keyPressEvent(event)

    def _nudge_selected(self, sx: int, sy: int) -> bool:
        """Move selected devices one snap step. One undo for the whole set."""
        items = [i for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not items:
            return False
        self.session.begin_edit("move")
        self.sync_positions()
        moved: set[str] = set()
        try:
            for item in items:
                nx = item.pos().x() + sx * item._snap
                ny = item.pos().y() + sy * item._snap
                self.session.move(item.component_id, nx, ny)
                comp = self.session.sheet().component(item.component_id)
                item.setPos(comp.x, comp.y)
                moved.add(item.component_id)
            if moved:
                self.session.drop_wire_routes(moved)
        finally:
            self.session.end_edit()
        if moved:
            self._redraw_wires()
            self.project_changed.emit()
            refs = ", ".join(sorted(moved))
            self.status.emit(f"moved {refs}")
        return True

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pin = None if is_layout_sheet(self.session.sheet()) else self._pin_at(event.position())
        wire = None if pin is not None else self._wire_at(event.position())
        device = None if pin is not None or wire is not None else self._device_at(event.position())
        if wire is not None and wire.wire_id:
            self.wire_label_requested.emit(wire.wire_id)
            event.accept()
            return
        if device is not None:
            self.object_edit_requested.emit(device.component_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        self.fill_context_menu(menu, event.pos())
        if menu.actions():
            menu.exec(event.globalPos())
        event.accept()

    def fill_context_menu(self, menu: QMenu, view_pos) -> None:
        """Populate *menu* for a right-click at view coordinates. Used by tests."""
        layout = is_layout_sheet(self.session.sheet())
        pin = None if layout else self._pin_at(view_pos)
        wire = None if pin is not None else self._wire_at(view_pos)
        device = None if pin is not None or wire is not None else self._device_at(view_pos)
        if device is None and pin is not None:
            device = self._device_at(view_pos)

        if device is not None:
            self.scene.clearSelection()
            device.setSelected(True)
            ref = device.component_id
            locked = True
            try:
                locked = self.session.sheet().component(ref).pins_locked
            except KeyError:
                locked = True
            if pin is not None:
                menu.addAction("Start wire here", lambda r=pin.ref(): self._start_wire_from(r))
                menu.addSeparator()
            menu.addAction("Object editor…", lambda r=ref: self.object_edit_requested.emit(r))
            menu.addAction("Rename…", lambda r=ref: self.rename_requested.emit(r))
            menu.addAction("Set address…", lambda r=ref: self.address_edit_requested.emit(r))
            menu.addAction("Duplicate", self.duplicate_selected)
            if not device.layout:
                menu.addAction("Rotate 90°", self.rotate_selected)
            menu.addSeparator()
            if locked:
                menu.addAction("Unlock connection points", lambda: self.set_selected_pins_locked(False))
            else:
                menu.addAction("Lock connection points", lambda: self.set_selected_pins_locked(True))
                menu.addAction("Reset connection points", self.reset_selected_pins)
            menu.addSeparator()
            menu.addAction("Bigger  ]", lambda: self.nudge_scale(1.1))
            menu.addAction("Smaller  [", lambda: self.nudge_scale(1 / 1.1))
            menu.addAction("Delete", self._delete_selected)
            return

        if wire is not None:
            wid = wire.wire_id
            menu.addAction("Set label…", lambda w=wid: self.wire_label_requested.emit(w))
            menu.addAction("Set wire number…", lambda w=wid: self.wire_number_requested.emit(w))
            menu.addAction("Set size, color, type…", lambda w=wid: self.wire_spec_requested.emit(w))
            found = None
            try:
                found = self.session.find_wire(wire_id=wid) if wid else None
            except (KeyError, ValueError):
                found = None
            if found is not None:
                if found.number_locked:
                    menu.addAction(
                        "Unlock wire number",
                        lambda w=wid: self._set_wire_locked(w, False),
                    )
                elif found.number:
                    menu.addAction(
                        "Lock wire number",
                        lambda w=wid: self._set_wire_locked(w, True),
                    )
            menu.addAction("Reset auto-route", lambda w=wid: self._reset_wire_route(w))
            menu.addAction(
                f"Delete wire {wid}" if wid else "Delete wire",
                lambda w=wid: self._delete_wire(w),
            )
            menu.addSeparator()

        if not layout:
            menu.addAction("Wire mode", self.set_wire_mode)
        menu.addAction("Revision cloud", self.set_cloud_mode)
        menu.addAction("Select", self.set_select_mode)
        menu.addAction("Fit page", self.fit_page)
        if layout:
            layout_action = menu.addAction("Build layout from schematic", self.pack_requested.emit)
            layout_action.setToolTip("Place catalog-sized devices onto the panel layout.")
        menu.addSeparator()
        menu.addAction("New page…", self.new_sheet_requested.emit)
        menu.addAction("Rename page…", self.rename_sheet_requested.emit)
        menu.addAction("Close this page", self.close_sheet_requested.emit)
        menu.addAction("Delete this page…", self.delete_sheet_requested.emit)

    def _start_wire_from(self, ref: str) -> None:
        if is_layout_sheet(self.session.sheet()):
            return
        self.set_wire_mode()
        self.wire_from = ref
        self._wire_from_at = None
        self.status.emit(f"wire from {ref} — click the other pin or rail (Esc cancels)")

    def _set_wire_locked(self, wire_id: str, locked: bool) -> None:
        if not wire_id:
            return
        try:
            self.session.set_wire_spec(wire_id, locked=locked)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"{'lock' if locked else 'unlock'} {wire_id}")

    def _reset_wire_route(self, wire_id: str) -> None:
        if not wire_id:
            return
        try:
            self.session.clear_wire_route(wire_id)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"auto-route {wire_id}")

    def _delete_wire(self, wire_id: str) -> None:
        if not wire_id:
            return
        try:
            self.session.delete_wire(wire_id)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"deleted wire {wire_id}")

    def _delete_middle_hit(self) -> None:
        """Middle-click (no drag) removes the device or wire under the cursor."""

        wire = self._middle_hit_wire
        device = self._middle_hit_device
        if wire is not None and wire.wire_id:
            self._delete_wire(wire.wire_id)
            return
        if device is None:
            return
        ref = device.component_id
        if self.wire_from and self.wire_from.startswith(ref + "."):
            self.cancel_wire()
        self.session.begin_edit("delete")
        removed = False
        try:
            self.sync_positions()
            try:
                if getattr(device, "strip_block", False):
                    self.session.delete_strip_component(ref)
                else:
                    self.session.delete_component(ref)
            except KeyError:
                return
            removed = True
        finally:
            self.session.end_edit()
        if not removed:
            return
        self.reload()
        self.status.emit(f"deleted {ref}")
        self.project_changed.emit()

    def _delete_selected(self) -> None:
        selected = list(self.scene.selectedItems())
        strip_refs = [i.component_id for i in selected if getattr(i, "strip_block", False) and i.component_id]
        refs = [i.component_id for i in selected if isinstance(i, ComponentItem)]
        wires = [i.wire_id for i in selected if isinstance(i, WireItem) and i.wire_id]
        if not refs and not wires and not strip_refs:
            return
        self.session.begin_edit("delete")
        try:
            self.sync_positions()
            seen: set[str] = set()
            for ref in strip_refs:
                if ref in seen:
                    continue
                seen.add(ref)
                try:
                    from panelcad.core.terminal_strip import remove_strip_component

                    remove_strip_component(self.session, ref)
                except KeyError:
                    continue
            for ref in refs:
                if ref in seen:
                    continue
                self.session.delete_component(ref)
            for wid in wires:
                try:
                    self.session.delete_wire(wid)
                except KeyError:
                    continue
        finally:
            self.session.end_edit()
        if refs or strip_refs:
            self.reload()
            count = len(set(refs) | set(strip_refs))
            noun = "object" if count == 1 else "objects"
            self.status.emit(f"deleted {count} {noun}")
        elif wires:
            self._redraw_wires()
            self.project_changed.emit()
            self.status.emit(f"deleted {len(wires)} wire(s)")

    def duplicate_selected(self) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        created = None
        self.session.begin_edit("duplicate")
        self.sync_positions()
        try:
            for ref in refs:
                created = self.session.duplicate_component(ref)
        finally:
            self.session.end_edit()
        if created is not None:
            self.reload()
            self.select_ref(created.id)
            self.status.emit(f"duplicated → {created.id}")

    def rotate_selected(self) -> None:
        refs = [
            i.component_id
            for i in self.scene.selectedItems()
            if isinstance(i, ComponentItem) and not i.layout
        ]
        last = None
        self.session.begin_edit("rotate")
        self.sync_positions()
        try:
            for ref in refs:
                last = self.session.rotate_component(ref, 90)
            if last is not None:
                self.session.drop_wire_routes(set(refs))
        finally:
            self.session.end_edit()
        if last is not None:
            self.reload()
            self.select_ref(last.id)
            self.status.emit(f"{last.id} rotation {last.rotation}°")

    def nudge_scale(self, factor: float) -> None:
        items = [item for item in self.scene.selectedItems() if isinstance(item, ComponentItem)]
        if not items:
            return
        self.session.begin_edit("scale")
        try:
            for item in items:
                item.apply_scale(item._scale * factor)
                item.commit_scale()
        finally:
            self.session.end_edit()


def _ignore_mouse(item: QGraphicsItem) -> None:
    item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
    item.setZValue(-10)
    if isinstance(item, (QGraphicsLineItem, QGraphicsRectItem)):
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)


LayoutView = SchematicView

_register_schematic_view_cls(SchematicView)
