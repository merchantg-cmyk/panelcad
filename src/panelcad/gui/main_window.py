from __future__ import annotations

import errno
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QStyle,
    QTabBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.cli.agent import Agent
from panelcad.gui.ai_dock import AiDock
from panelcad.gui.canvas import ComponentItem, SchematicView
from panelcad.gui.bom_dock import BomDock
from panelcad.gui.quote_dock import QuoteDock
from panelcad.gui.docks import LibraryDock, PropertiesDock
from panelcad.gui.power_dock import PowerDock
from panelcad.gui.problems import LiveCheckRunner, ProblemsDock
from panelcad.gui.sizing_panel import SizingPanel
from panelcad.gui.sheet_bar import SheetBar
from panelcad.version import __version__
from shiboken6 import isValid


NARROW_WINDOW_PX = 1280
WIDE_LIBRARY_PX = 360


class _DockSeparatorStyle(QProxyStyle):
    """Splitter thickness in logical pixels. 100%, 125%, and 150% all stay grabbable."""

    EXTENT = 8

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric == QStyle.PixelMetric.PM_DockWidgetSeparatorExtent:
            return self.EXTENT
        return super().pixelMetric(metric, option, widget)


_DOCK_STYLE: QStyle | None = None


def _shared_dock_style() -> QStyle:
    """One splitter proxy for every window, owned by the application.

    ``QWidget.setStyle`` does not take ownership, and with an application style
    sheet Qt wraps the proxy in a QStyleSheetStyle that keeps a raw pointer to
    it. A proxy parented to the window is its first child, so ``~QWidget``
    deleted it before the docks and the canvas. Their teardown then called
    through the freed proxy: 0xC0000005 in Qt6Widgets.dll on Windows, inside
    ``MainWindow.event`` on DeferredDelete or whenever the wrapper was collected.
    """

    global _DOCK_STYLE
    style = _DOCK_STYLE
    if style is None or not isValid(style):
        style = _DockSeparatorStyle()
        app = QApplication.instance()
        if app is not None:
            style.setParent(app)
        _DOCK_STYLE = style
    return style


def _release_dock_contents(widget: QWidget) -> None:
    """Let the splitter move. A form's size hint must not become a fixed size.

    Preferred keeps the hint as a minimum, so the handle stops. Ignored lets
    the dock be shorter or narrower than the form, and the form scrolls or clips.
    """

    widget.setMinimumSize(0, 0)
    widget.setMaximumSize(16777215, 16777215)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)


class _ToolDock(QDockWidget):
    """Library and bottom tools. Their contents must not freeze the splitter."""

    FLOOR = QSize(64, 48)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setMinimumSize(self.FLOOR)
        self.setMaximumSize(16777215, 16777215)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # QMainWindow sizes the splitter from this, not from the form inside.
        return QSize(self.FLOOR)

    def setWidget(self, widget: QWidget | None) -> None:  # noqa: N802
        if widget is not None:
            _release_dock_contents(widget)
        super().setWidget(widget)
_LAYOUT_FIX_CHECKS = frozenset(
    {
        "layout_not_ready",
        "placeholder_footprint",
        "catalog_missing_dimensions",
        "reconstruct_generic_layout",
        "missing_footprint",
        "missing_part_number",
    }
)
_FIX_NEXT_STEP = {
    "outside_enclosure": (
        "Move {ref} inside the enclosure, or make the enclosure larger "
        "(Tools → Panel → Size Enclosure…)."
    ),
    "terminal_not_on_strip": "Add {ref} to a terminal strip (Strip → Open strip…).",
    "reconstruct_print_unextracted": (
        "Read the old drawing with File → Import print…, then compare it with the photos."
    ),
}
_LAYOUT_FIX_ACTIONS = frozenset({"pick_catalog", "needs_info", "confirm_envelope"})


def _unplaced_layout_message(rows: list[dict]) -> str:
    """Dialog text for parts Build layout left off the sheet."""

    reasons = [str(row.get("reason") or row.get("ref") or "").strip() for row in rows]
    reasons = [line for line in reasons if line]
    shown = reasons[:10]
    extra = len(rows) - len(shown)
    noun = "part was" if len(rows) == 1 else "parts were"
    lines = [f"{len(rows)} {noun} not placed:", *shown]
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "\n".join(lines)


def _qt_alive(obj: object) -> bool:
    """False once Qt has deleted the C++ object behind this wrapper."""

    if obj is None:
        return False
    try:
        return bool(isValid(obj))
    except (RuntimeError, TypeError):
        return False


def _dock_title(dock: QDockWidget) -> str:
    if not _qt_alive(dock):
        return ""
    try:
        return dock.windowTitle().replace("&", "")
    except RuntimeError:
        return ""


def _tab_titles(bar: QTabBar) -> set[str]:
    if not _qt_alive(bar):
        return set()
    try:
        titles = {bar.tabText(index).replace("&", "") for index in range(bar.count())}
    except RuntimeError:
        return set()
    titles.discard("")
    return titles


def default_save_directory() -> Path:
    """Folder Save As offers for a drawing. Never the program directory."""

    folder = Path.home() / "Documents" / "PanelCAD"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home() / "Documents"
    return folder


def cant_save_message(exc: OSError) -> str:
    """Plain text for a save that failed for a reason other than a busy file."""

    reason = "PanelCAD can't write to this folder."
    if isinstance(exc, NotADirectoryError):
        reason = "That path is not a folder."
    elif isinstance(exc, FileNotFoundError):
        reason = "That drive or folder is missing."
    else:
        err = getattr(exc, "errno", None)
        if err == errno.ENOSPC:
            reason = "The disk is full."
        elif err == errno.ENAMETOOLONG:
            reason = "That name is too long."
    return (
        "Can't save there. "
        f"{reason} "
        "Nothing was overwritten. "
        "Use File → Save As to pick another folder."
    )


def save_as_start(current: Path | None) -> str:
    """Start path for Save As. A file already under Documents stays there."""

    documents = default_save_directory()
    if current is None:
        return str(documents)
    try:
        current.resolve().relative_to((Path.home() / "Documents").resolve())
        return str(current)
    except (OSError, ValueError):
        return str(documents / current.name)


class _StartupUpdateBridge(QObject):
    """Carries a startup feed result back to the window thread."""

    finished = Signal(object)


def _shop_warning(line: str) -> str:
    """Name the Size Enclosure menu. The status bar is not the agent API."""

    return line.replace("recommend_enclosure", "Size Enclosure")


def circuit_warning_status_text(warnings: list[str]) -> str:
    """Status-bar text for a Circuit Builder insert. Routine notes stay here."""

    lines = [_shop_warning(str(raw).strip()) for raw in warnings if str(raw).strip()]
    return "; ".join(lines)


def circuit_warning_dialog_text(warnings: list[str]) -> str | None:
    """Modal text for a Circuit Builder insert.

    A no-fit warning names Tools → Size Enclosure. Routine notes (an end stop
    was added) stay on the status bar and do not open a message box.
    """

    shown: list[str] = []
    for raw in warnings:
        line = str(raw).strip()
        if not line:
            continue
        low = line.lower()
        if "not fit" not in low and "free backplate" not in low:
            continue
        shown.append(_shop_warning(line))
    if not shown:
        return None
    return "\n".join(shown[:12])


def _choose_template(parent: QWidget):
    """Pick a built-in drawing. Returns a Session, or None when cancelled."""

    from panelcad.examples.dol import build_dol
    from panelcad.examples.plc_io import build_plc_motor_io
    from panelcad.examples.power_budget import build_plc_hoa
    from panelcad.examples.xref_demo import build_iec_mirror, build_nema_seal_in

    choices = (
        ("DOL motor starter", build_dol),
        ("NEMA seal-in", build_nema_seal_in),
        ("IEC contact mirror", build_iec_mirror),
        ("PLC motor I/O", build_plc_motor_io),
        ("PLC HOA power", build_plc_hoa),
    )
    names = [label for label, _builder in choices]
    name, ok = QInputDialog.getItem(parent, "New from template", "Template", names, 0, False)
    if not ok:
        return None
    for label, builder in choices:
        if label == name:
            return builder()
    return None


class _InspectorWidth(QObject):
    """Keep the inspector as wide as the dock, so rows wrap instead of scrolling sideways."""

    def __init__(self, scroll, body: QWidget) -> None:
        super().__init__(scroll)
        self._scroll = scroll
        self._body = body

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            width = self._scroll.viewport().width()
            if width > 0:
                self._body.setMinimumWidth(0)
                self._body.setMaximumWidth(width)
        return False


# 50 ms beats while the splash or a startup dialog is up. Two seconds is enough
# for the off-screen paint. present_startup_prompts schedules one real pass
# when that wait ends, so a long recovery dialog does not depend on the cap.
_STARTUP_UPDATE_POLLS = 40


def about_text(version: str) -> str:
    return (
        f"PanelCAD {version}: control-panel drawings, BOM, and build package.\n\n"
        "Help → Report a bug… if something goes wrong."
    )


class MainWindow(QMainWindow):
    def __init__(self, session: Session | None = None, *, show_start: bool = False) -> None:
        super().__init__()
        # Free the C++ window, its docks, timers and children once it closes.
        # Nothing touches the window after closeEvent; app.py's startup timer
        # uses the window as its context so it is dropped with it.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        from panelcad.gui import gc_control

        gc_control.install()
        # QProxyStyle() with no base does not delete the application style on the
        # way out; handing it the app style does, and the next window crashes.
        # The proxy must outlive this window. See ``_shared_dock_style``.
        self.setStyle(_shared_dock_style())
        self._hiding_dock_tabs = False
        self._hide_dock_tabs_again = False
        self._closing = False
        self._switching_sheet = False
        self._suspend_guidance_persist = True
        # One timer for every show/resize. A new singleShot per event can run
        # while a dock tab bar is still being destroyed.
        self._dock_tab_timer = QTimer(self)
        self._dock_tab_timer.setSingleShot(True)
        self._dock_tab_timer.timeout.connect(self._hide_duplicate_dock_tabs_deferred)
        self.session = session or Session.create("Untitled Panel")
        self.setWindowTitle(self._title())
        self.setMinimumSize(960, 540)
        self._place_initial_window()

        self.canvas = SchematicView(self.session, self)
        self.sheet_bar = SheetBar()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        from panelcad.gui.guidance import EmptyState, HintBanner

        self._hint_banner = HintBanner()
        self._hint_banner.learn_more.connect(self.show_help)
        self._sheet_empty = EmptyState()
        self._sheet_empty.clicked.connect(self._empty_sheet_action)
        layout.addWidget(self.sheet_bar)
        layout.addWidget(self._hint_banner)
        layout.addWidget(self._sheet_empty)
        layout.addWidget(self.canvas, 1)
        self._editor = central
        self._start = None
        self._stack = None
        self._show_start_on_open = show_start
        if show_start:
            from PySide6.QtWidgets import QStackedWidget

            from panelcad.gui.start_page import StartPage

            self._start = StartPage()
            self._start.new_blank.connect(self.new_project)
            self._start.new_template.connect(self.load_example)
            self._start.circuit_builder.connect(self.insert_circuit)
            self._start.open_project.connect(self.open_project)
            self._start.import_print.connect(self.import_print)
            self._start.reconstruct_photos.connect(self.reconstruct_from_photos)
            # Same list as File > Open Recent. Splash and the startup dialogs
            # stay on their own branch; this only redirects the existing row.
            self._start.recent_opened.connect(self._open_recent)
            self._start.open_sample.connect(self.open_sample_project)
            self._start.tour_requested.connect(self._restart_tour)
            self._stack = QStackedWidget()
            self._stack.addWidget(self._start)
            self._stack.addWidget(self._editor)
            self.setCentralWidget(self._stack)
        else:
            self.setCentralWidget(self._editor)

        # Right docks own the bottom-right corner so Inspector|AI tabs stay in
        # that column and never sprout a second tab bar on the Problems strip.
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)

        self.library_dock = LibraryDock(self.session.catalog)
        self.lib_wrap = _ToolDock("Library", self)
        self.lib_wrap.setObjectName("libraryDock")
        self.lib_wrap.setWidget(self.library_dock)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.lib_wrap)

        self.props = PropertiesDock(
            on_change=self._title_block_changed,
            on_logo=self.insert_logo,
            on_clear_logo=self.clear_logo,
            on_component_change=self._component_visuals,
        )
        self.prop_wrap = _ToolDock("Inspector", self)
        self.prop_wrap.setObjectName("inspectorDock")
        prop_scroll = QScrollArea()
        prop_scroll.setWidgetResizable(True)
        prop_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        prop_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        prop_scroll.setWidget(self.props)
        self._prop_scroll = prop_scroll
        self._inspector_fit = _InspectorWidth(prop_scroll, self.props)
        prop_scroll.viewport().installEventFilter(self._inspector_fit)
        self.prop_wrap.setWidget(prop_scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.prop_wrap)

        self.ai_dock = AiDock()
        self.ai_dock.prompt_submitted.connect(self._submit_ai_prompt)
        self.ai_wrap = _ToolDock("AI", self)
        self.ai_wrap.setObjectName("aiDock")
        self.ai_wrap.setWidget(self.ai_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_wrap)
        self.tabifyDockWidget(self.prop_wrap, self.ai_wrap)
        self.ai_wrap.raise_()

        self.bom = BomDock(
            on_export=self.export_bom,
            on_export_grouped=self.export_bom_grouped,
            on_import=self.import_csv,
            on_select_ref=self.canvas.select_ref,
        )
        self.bom_wrap = _ToolDock("BOM", self)
        self.bom_wrap.setObjectName("bomDock")
        self.bom_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.bom_wrap.setWidget(self.bom)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.bom_wrap)
        self.resizeDocks([self.bom_wrap], [120], Qt.Orientation.Vertical)
        if self.width() >= NARROW_WINDOW_PX:
            self.resizeDocks([self.lib_wrap], [WIDE_LIBRARY_PX], Qt.Orientation.Horizontal)
        else:
            self.resizeDocks([self.lib_wrap], [220], Qt.Orientation.Horizontal)
        self.resizeDocks([self.prop_wrap], [260], Qt.Orientation.Horizontal)
        self.bom.set_session(self.session)

        self.problems = ProblemsDock()
        self.problems_wrap = _ToolDock("Problems", self)
        self.problems_wrap.setObjectName("problemsDock")
        self.problems_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.problems_wrap.setWidget(self.problems)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.problems_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.problems_wrap)

        from panelcad.gui.photo_viewer import PhotoPeekDock

        self.photo_peek = PhotoPeekDock()
        self.photo_wrap = _ToolDock("Photo", self)
        self.photo_wrap.setObjectName("photoDock")
        self.photo_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.photo_wrap.setWidget(self.photo_peek)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.photo_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.photo_wrap)
        self.canvas.selection_changed.connect(self._peek_reconstruct_photo)

        self.power = PowerDock()
        self.power_wrap = _ToolDock("Power", self)
        self.power_wrap.setObjectName("powerDock")
        self.power_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.power_wrap.setWidget(self.power)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.power_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.power_wrap)
        self.power.set_session(self.session)
        self.power.export_requested.connect(self.export_power_budget)
        self.canvas.project_changed.connect(self.power.refresh)

        self.quote = QuoteDock()
        self.quote_wrap = _ToolDock("Quote", self)
        self.quote_wrap.setObjectName("quoteDock")
        self.quote_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.quote_wrap.setWidget(self.quote)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.quote_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.quote_wrap)
        self.bom_wrap.raise_()
        self.quote.set_session(self.session)

        self.sizing_panel = SizingPanel()
        self.sizing_wrap = _ToolDock("Motor sizing", self)
        self.sizing_wrap.setObjectName("sizingDock")
        self.sizing_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.sizing_wrap.setWidget(self.sizing_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.sizing_wrap)
        self.tabifyDockWidget(self.problems_wrap, self.sizing_wrap)
        self.bom_wrap.raise_()

        from panelcad.gui.guidance import NextStepsPanel

        self.next_steps = NextStepsPanel()
        self.next_steps.do_requested.connect(self._do_next_step)
        self.next_steps.dismissed.connect(self._hide_next_steps)
        self.next_steps_wrap = _ToolDock("Next steps", self)
        self.next_steps_wrap.setObjectName("nextStepsDock")
        self.next_steps_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.next_steps_wrap.setWidget(self.next_steps)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.next_steps_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.next_steps_wrap)
        self.bom_wrap.raise_()
        self.next_steps.set_session(self.session)

        from panelcad.gui.troubleshoot_dock import TroubleshootDock

        self.troubleshoot = TroubleshootDock()
        self.troubleshoot.highlight_requested.connect(self._highlight_troubleshoot)
        self.troubleshoot.status_message.connect(self._status)
        self.troubleshoot.settings_requested.connect(self._troubleshoot_settings)
        self.troubleshoot_wrap = _ToolDock("Troubleshoot", self)
        self.troubleshoot_wrap.setObjectName("troubleshootDock")
        self.troubleshoot_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.troubleshoot_wrap.setWidget(self.troubleshoot)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.troubleshoot_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.troubleshoot_wrap)
        self.bom_wrap.raise_()
        self.troubleshoot.set_session(self.session)
        self.troubleshoot.help_requested.connect(self.show_help)

        from panelcad.gui.import_review import ImportReviewPanel

        self.import_review = ImportReviewPanel()
        self.import_review_wrap = _ToolDock("Print review", self)
        self.import_review_wrap.setObjectName("printReviewDock")
        self.import_review_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.import_review_wrap.setWidget(self.import_review)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.import_review_wrap)
        self.tabifyDockWidget(self.problems_wrap, self.import_review_wrap)
        self.import_review.decision_made.connect(self._import_decision)
        self.import_review.rerun_requested.connect(self._import_rerun)
        self._import_job = None
        # Print review stays closed until an import. Its crop panes are tall
        # enough to squeeze the sheet when the dock opens on an empty project.
        self.import_review_wrap.hide()
        self._unlock_tool_docks()
        if self.bom_wrap.isVisible():
            self.resizeDocks([self.bom_wrap], [120], Qt.Orientation.Vertical)

        self._check_runner = LiveCheckRunner(self.session, self)
        self._checks_tick = 0
        self._check_runner.rows_ready.connect(self._on_checks)
        self.canvas.project_changed.connect(self._check_runner.schedule)
        self.canvas.finding_clicked.connect(self._jump_finding)
        self.problems.jump_requested.connect(self._jump_finding)
        self.problems.fix_requested.connect(self._fix_finding)
        self.problems.suppress_requested.connect(self._suppress_finding)
        self.problems.unsuppress_requested.connect(self._unsuppress_finding)
        self.problems.export_requested.connect(self.export_checks)
        self._check_runner.schedule()

        self._agent = Agent()
        self._yaml_mtime = 0.0
        self._disk_conflict_noted = False
        self._ai_idle_ticks = 0
        self._bind_ai()
        self._fitted_once = False
        self._layout_applied = False
        self._narrow_docks_on = False
        self._applying_docks = False
        self._ai_timer = QTimer(self)
        self._ai_timer.setInterval(400)
        self._ai_timer.timeout.connect(self._poll_ai)
        self._ai_timer.start()
        self._share_timer = QTimer(self)
        self._share_timer.setInterval(60_000)
        self._share_timer.timeout.connect(self._heartbeat_share_lock)
        self._share_timer.start()

        self.library_dock.device_chosen.connect(self.canvas.set_place_mode)
        self.library_dock.place_now_requested.connect(self._place_from_library)
        self.library_dock.new_requested.connect(self._new_library_device)
        self.library_dock.edit_requested.connect(self._edit_library_device_in_place)
        self.library_dock.duplicate_requested.connect(self._duplicate_library_device)
        self.library_dock.parts_requested.connect(self.browse_catalog_parts)
        self.props.select_requested.connect(self.canvas.select_ref)
        self.props.edit_requested.connect(self.edit_object)
        self.props.catalog_requested.connect(self.assign_catalog_part)
        self.props.envelope_requested.connect(self.confirm_part_envelope)
        self.props.address_requested.connect(self.edit_address)
        self.props.delete_requested.connect(self._delete_ref)
        self.bom.select_requested.connect(self.canvas.select_ref)
        self.bom.place_on_sheet_requested.connect(self._place_from_bom)
        self.bom.edit_requested.connect(self.edit_object)
        self.bom.address_requested.connect(self.edit_address)
        self.bom.delete_requested.connect(self._delete_ref)
        self.bom.oem_requested.connect(self._oem_update)
        self.bom.oem_all_requested.connect(self._oem_update_all)
        self.bom.catalog_requested.connect(self._add_from_catalog)
        self.bom.resolve_requested.connect(self._needs_action)
        self.bom.lookup_all_requested.connect(self._lookup_all_needs)
        self.problems.print_requested.connect(self.print_package)
        self.canvas.status.connect(self._status)
        self.canvas.selection_changed.connect(self._on_selection)
        self.canvas.logo_clicked.connect(self.insert_logo)
        self.canvas.project_changed.connect(self.bom.refresh)
        self.canvas.project_changed.connect(self._on_drawing_changed)
        self.canvas.object_edit_requested.connect(self.edit_object)
        self.canvas.rename_requested.connect(self.rename_device)
        self.canvas.address_edit_requested.connect(self.edit_address)
        self.canvas.wire_label_requested.connect(self.edit_wire_label)
        self.canvas.wire_number_requested.connect(self.edit_wire_number)
        self.canvas.wire_spec_requested.connect(self.edit_wire_spec)
        self.canvas.delete_sheet_requested.connect(self.delete_sheet)
        self.canvas.close_sheet_requested.connect(self.close_sheet)
        self.canvas.new_sheet_requested.connect(self.new_sheet)
        self.canvas.rename_sheet_requested.connect(self.rename_sheet)
        self.canvas.pack_requested.connect(self.pack_from_schematic)
        self.sheet_bar.delete_requested.connect(self.delete_sheet_id)
        self.sheet_bar.close_requested.connect(self.close_sheet_id)
        self.sheet_bar.reopen_requested.connect(self.reopen_sheet_id)
        self.sheet_bar.rename_requested.connect(self.rename_sheet_id)
        self.sheet_bar.move_requested.connect(self.move_sheet_id)
        self.sheet_bar.order_changed.connect(self.reorder_open_sheets)
        self.sheet_bar.sheet_selected.connect(self._switch_sheet)
        self.sheet_bar.panel_selected.connect(self._choose_active_panel)
        self.sheet_bar.add_requested.connect(self.new_sheet)
        self.sheet_bar.set_session(self.session)

        self._toolbar_icons: list[tuple[QAction, str]] = []
        self._theme_actions: dict[str, QAction] = {}
        self._canvas_actions: dict[str, QAction] = {}
        self._make_menus()
        self._make_toolbar()
        bar = QStatusBar()
        self.setStatusBar(bar)
        self._make_status_widgets()
        self.canvas.view_changed.connect(self._refresh_status_widgets)
        self._status(
            "Ready — right-click a part, wire, tab, or empty sheet. "
            "Hover a terminal and click or drag to the next one."
        )
        self.props.show_project(self.session)
        self._startup_update_bridge = _StartupUpdateBridge(self)
        self._startup_update_bridge.finished.connect(self._on_startup_update_report)
        self._saved_snapshot = ""
        self._remember_saved_project()
        from panelcad.gui.splash import splash_is_visible

        # While the splash is up, the update notice waits for the open /
        # recovery / welcome pass. launch_gui clears this when that pass ends.
        self._defer_startup_updates = splash_is_visible()
        self._startup_update_waits = 0
        self._startup_updates_ran = False
        QTimer.singleShot(400, self, self._startup_updates)
        # One stylesheet after the docks exist. A sheet applied earlier, or
        # skipped on the next window, leaves saveState/restoreState with two
        # bottom tab bars. reapply does not subscribe the theme hooks twice.
        from panelcad.gui.theme import install
        from panelcad.theme import subscribe

        install(reapply=True)
        subscribe(self.refresh_theme)
        self._sync_theme_checks()
        self._refresh_status_widgets()
        if show_start:
            self._show_start()
        else:
            self._apply_next_steps_pref()
        self._install_learning()
        self._suspend_guidance_persist = False
        self._sync_empty_states()

    def _place_initial_window(self) -> None:
        """First-run size stays on the screen. A saved dock layout does not store this."""

        from PySide6.QtGui import QGuiApplication

        screen = self.screen()
        if screen is None:
            app = QGuiApplication.instance()
            screen = app.primaryScreen() if app is not None else None
        if screen is None:
            self.resize(1400, 860)
            return
        avail = screen.availableGeometry()
        width = min(1400, int(avail.width() * 0.9))
        height = min(860, int(avail.height() * 0.9))
        self.resize(max(1, width), max(1, height))
        frame = self.frameGeometry()
        frame.moveCenter(avail.center())
        self.move(frame.topLeft())

    def _title(self) -> str:
        name = self.session.project.name
        recovered = " — recovered" if getattr(self.session, "recovered", False) else ""
        read_only = " — read-only" if getattr(self.session, "read_only", False) else ""
        path = f" — {self.session.path}" if self.session.path else ""
        return f"PanelCAD {__version__} — {name}{recovered}{read_only}{path}"

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        self._refresh_status_widgets()
        self._sync_ai_page()

    def _make_status_widgets(self) -> None:
        self._sheet_label = QLabel()
        self._zoom_label = QLabel()
        self._grid_label = QLabel()
        self._units_label = QLabel("Units  mm")
        self._checks_label = QLabel("Checks  0")
        self._saved_label = QLabel("")
        self._saved_label.setObjectName("lastSavedLabel")
        for label in (
            self._sheet_label,
            self._zoom_label,
            self._grid_label,
            self._units_label,
            self._checks_label,
            self._saved_label,
        ):
            self.statusBar().addPermanentWidget(label)
        self._learn_more = QPushButton("Learn more")
        self._learn_more.setObjectName("statusLearnMore")
        self._learn_more.setFlat(True)
        self._learn_more.hide()
        self._learn_more.clicked.connect(self._open_status_help)
        self._status_help_topic = ""
        self.statusBar().addPermanentWidget(self._learn_more)

    def _refresh_status_widgets(self) -> None:
        if not hasattr(self, "_sheet_label"):
            return
        try:
            from panelcad.core.layout import is_layout_sheet
            from panelcad.gui.canvas import PANEL_SNAP, SNAP

            sheet = self.session.sheet()
            self._sheet_label.setText(f"Sheet  {sheet.title or sheet.id}")
            on_start = self._stack is not None and self._stack.currentWidget() is self._start
            for label in (self._sheet_label, self._grid_label, self._units_label, self._checks_label):
                label.setVisible(not on_start)
            self._zoom_label.setVisible(not on_start)
            if on_start:
                self._saved_label.hide()
                self.statusBar().clearMessage()
                return
            self._zoom_label.setText(f"Zoom  {self.canvas.zoom_percent()}%")
            snap = PANEL_SNAP if is_layout_sheet(sheet) else SNAP
            self._grid_label.setText(f"Grid  10 mm    Snap  {snap:g} mm")
            self._units_label.setText("Units  mm")
            counts = self.problems.counts()
            n = counts["error"] + counts["warning"] + counts["info"]
            self._checks_label.setText(f"Checks  {n}")
            from panelcad.io.share import last_saved_text

            saved = last_saved_text(self.session.project)
            if getattr(self.session, "read_only", False):
                holder = getattr(self.session, "lock_holder", None)
                if holder is not None and not getattr(holder, "unreadable", False):
                    extra = f"Read-only · {holder.user or 'someone'} on {holder.host or 'another PC'}"
                else:
                    extra = "Read-only"
                saved = f"{saved}  ·  {extra}" if saved else extra
            self._saved_label.setText(saved)
            self._saved_label.setVisible(bool(saved) and not on_start)
        except RuntimeError:
            return

    def _make_toolbar(self) -> None:
        from panelcad.gui.theme import themed_icon

        bar = QToolBar("Main", self)
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        bar.setIconSize(QSize(20, 20))
        self.addToolBar(bar)
        self.toolbar = bar
        groups = (
            (
                ("New", "file-plus"),
                ("Open…", "folder-open"),
                ("Save", "save"),
            ),
            (
                ("Undo", "undo-2"),
                ("Redo", "redo-2"),
            ),
            (
                ("Select", "mouse-pointer-2"),
                ("Wire", "cable"),
            ),
            (
                ("Zoom in", "zoom-in"),
                ("Zoom out", "zoom-out"),
                ("Fit page", "expand"),
            ),
            (
                ("Print…", "printer"),
                ("Circuit Builder…", "workflow"),
                ("Settings…", "settings"),
            ),
        )
        for index, group in enumerate(groups):
            if index:
                bar.addSeparator()
            for label, icon in group:
                action = self._find_action(label)
                if action is None and label == "Zoom in":
                    action = self._act("Zoom in", None, lambda: self.canvas.zoom_by(1.15))
                elif action is None and label == "Zoom out":
                    action = self._act("Zoom out", None, lambda: self.canvas.zoom_by(1 / 1.15))
                if action is None:
                    continue
                action.setIcon(themed_icon(icon))
                tip = action.text().replace("&", "")
                action.setToolTip(tip)
                action.setStatusTip(tip)
                self._toolbar_icons.append((action, icon))
                bar.addAction(action)
        bar.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        from panelcad.gui.guidance import apply_action_guidance

        apply_action_guidance(self, self._on_action_hover)
        self._refresh_command_state()

    def _find_action(self, label: str) -> QAction | None:
        for action in self.findChildren(QAction):
            if action.text().replace("&", "") == label:
                return action
        return None

    def _recolor_toolbar(self) -> None:
        from panelcad.gui.theme import themed_icon

        for action, name in self._toolbar_icons:
            action.setIcon(themed_icon(name))

    def _save_appearance(self, write) -> None:
        try:
            write()
        except OSError as exc:
            self._status(f"could not save settings: {exc}")
            self._sync_theme_checks()

    # Bound slots instead of lambdas that capture ``self``: a lambda holds the
    # window from the sender's connection; a bound slot is dropped with it.
    def _restart_tour(self) -> None:
        self.start_tour(True)

    def _new_library_device(self) -> None:
        self.edit_library_device(None)

    def _edit_library_device_in_place(self, library_id: str) -> None:
        self.edit_library_device(library_id, duplicate=False)

    def _duplicate_library_device(self, library_id: str) -> None:
        self.edit_library_device(library_id, duplicate=True)

    def _lookup_all_needs(self) -> None:
        self._needs_action("lookup_all", "")

    def _sender_action_data(self) -> str | None:
        action = self.sender()
        data = action.data() if isinstance(action, QAction) else None
        return str(data) if data is not None else None

    def _theme_action_triggered(self, _checked: bool = False) -> None:
        name = self._sender_action_data()
        if name is not None:
            self._choose_theme(name)

    def _canvas_action_triggered(self, _checked: bool = False) -> None:
        name = self._sender_action_data()
        if name is not None:
            self._choose_canvas(name)

    def _reopen_sheet_action_triggered(self, _checked: bool = False) -> None:
        sheet_id = self._sender_action_data()
        if sheet_id is not None:
            self.reopen_sheet_id(sheet_id)

    def _choose_theme(self, name: str) -> None:
        from panelcad.theme import set_theme

        self._save_appearance(lambda: set_theme(name))

    def _choose_canvas(self, name: str) -> None:
        from panelcad.theme import set_canvas

        self._save_appearance(lambda: set_canvas(name))

    def _choose_phase(self, checked: bool) -> None:
        from panelcad.theme import set_phase_colors

        self._save_appearance(lambda: set_phase_colors(bool(checked)))

    def _sync_theme_checks(self) -> None:
        from panelcad.theme import get_canvas_choice, get_theme, phase_colors_enabled

        theme = get_theme()
        canvas = get_canvas_choice()
        for key, action in self._theme_actions.items():
            action.blockSignals(True)
            action.setChecked(key == theme)
            action.blockSignals(False)
        for key, action in self._canvas_actions.items():
            action.blockSignals(True)
            action.setChecked(key == canvas)
            action.blockSignals(False)
        phase = getattr(self, "_phase_action", None)
        if phase is not None:
            phase.blockSignals(True)
            phase.setChecked(phase_colors_enabled())
            phase.blockSignals(False)

    def refresh_theme(self) -> None:
        """Live theme and sheet colors. Keeps the current zoom."""

        try:
            from shiboken6 import isValid
        except Exception:  # noqa: BLE001
            isValid = None  # type: ignore[assignment]
        if isValid is not None and not isValid(self):
            from panelcad.theme import unsubscribe

            unsubscribe(self.refresh_theme)
            return
        try:
            from panelcad.gui.theme import canvas_qcolor

            self._sync_theme_checks()
            self.bom.apply_theme()
            self.quote.apply_theme()
            self.ai_dock.apply_theme()
            self.problems.apply_theme()
            self.power.apply_theme()
            self.import_review.apply_theme()
            self._recolor_toolbar()
            self.canvas.setBackgroundBrush(QBrush(canvas_qcolor("paper")))
            selected = self.canvas.selected_refs()
            self.canvas.reload()
            self.canvas.restore_selection(selected)
            self._refresh_status_widgets()
            if self._start is not None:
                self._start.refresh()
        except RuntimeError:
            from panelcad.theme import unsubscribe

            unsubscribe(self.refresh_theme)

    def open_settings(self) -> None:
        from panelcad.gui.settings_dialog import SettingsDialog

        SettingsDialog(self, self.session).exec()

    def _on_start_page(self) -> bool:
        return self._stack is not None and self._start is not None and self._stack.currentWidget() is self._start

    def _tool_docks(self) -> list[QDockWidget]:
        """Library, Inspector, AI, BOM, Problems, Power, Quote, and any later dock.

        The Help panel stays out of this list so opening a drawing does not
        pop it open for someone who did not ask for it.
        """

        docks = self.findChildren(QDockWidget)
        help_wrap = getattr(self, "help_wrap", None)
        if help_wrap is not None:
            docks = [dock for dock in docks if dock is not help_wrap]
        return docks

    def _show_start(self) -> None:
        if self._stack is None or self._start is None:
            return
        self._start.refresh()
        self._stack.setCurrentWidget(self._start)
        self.toolbar.hide()
        for dock in self._tool_docks():
            dock.hide()

    def _show_editor(self) -> None:
        if self._stack is None or self._editor is None:
            return
        if self._stack.currentWidget() is self._editor and self.toolbar.isVisible():
            return
        self._stack.setCurrentWidget(self._editor)
        self.toolbar.show()
        for dock in self._tool_docks():
            dock.show()
        if not self._layout_applied:
            if not self._restore_layout():
                self._apply_default_docks()
            self._layout_applied = True
        self._hide_print_review_until_import()
        self._refresh_status_widgets()
        QTimer.singleShot(0, self.canvas, self.canvas.fit_drawing)

    def open_path(self, path: str) -> None:
        try:
            opened = Session.open(path)
        except Exception as exc:  # noqa: BLE001
            from panelcad.io.yaml_format import describe_open_error

            QMessageBox.critical(self, "Open failed", describe_open_error(exc, path))
            return
        if not self._resolve_share_open(opened):
            opened.release_share_lock()
            return
        copies = list(opened.conflict_copies)
        notice = opened.share_notice
        self.session = opened
        self._reattach()
        self._show_editor()
        self._remember_saved_project()
        self._disk_conflict_noted = False
        if copies:
            from panelcad.gui.share_dialog import warn_conflict_copies

            warn_conflict_copies(self, copies)
        note = self._remember_recent(path)
        message = " — ".join(part for part in (notice, note) if part)
        if message:
            self._status(message)
        else:
            self.setWindowTitle(self._title())
            self._refresh_status_widgets()

    def _fill_recent_menu(self) -> None:
        from panelcad.gui.recent_menu import fill_recent_menu

        fill_recent_menu(
            self._recent_menu,
            on_open=self._open_recent,
            on_clear=self._clear_recent,
            on_hover=self.statusBar().showMessage,
        )

    def _open_recent(self, path: str) -> None:
        """Open a remembered project. A missing file gets a plain message, not a traceback."""

        from panelcad.io.winpath import project_presence

        try:
            presence = project_presence(path)
        except Exception:  # noqa: BLE001 — the menu must not dump a stack
            presence = "unavailable"
        if presence == "missing":
            self._missing_recent(path)
            return
        self.open_path(path)

    def _missing_recent(self, path: str) -> None:
        from panelcad.io.winpath import display_name
        from panelcad.theme import forget_project

        name = display_name(path) or path
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("File not found")
            box.setText(f"Can't find {name}.")
            box.setInformativeText(path)
            remove = box.addButton("Remove from recent", QMessageBox.ButtonRole.AcceptRole)
            close = box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(close)
            box.setEscapeButton(close)
            box.exec()
            dropped = box.clickedButton() is remove
        except Exception:  # noqa: BLE001 — a dialog failure is still a plain status line
            self.statusBar().showMessage(f"Can't find {name}.")
            return
        if not dropped:
            self.statusBar().showMessage(f"Can't find {name}.")
            return
        try:
            forget_project(path)
        except OSError as exc:
            self._status(f"could not save settings: {exc}")
            return
        if self._start is not None:
            self._start.refresh()
        self.statusBar().showMessage(f"Removed {name} from recent projects")

    def _clear_recent(self) -> None:
        from panelcad.theme import clear_recent_projects

        try:
            clear_recent_projects()
        except OSError as exc:
            self._status(f"could not save settings: {exc}")
            return
        if self._start is not None:
            self._start.refresh()
        self.statusBar().showMessage("Recent projects cleared")

    def _remember_recent(self, path: str) -> str:
        """Add ``path`` to the recent list. A locked settings file must not abort Open or Save."""

        from panelcad.theme import remember_project

        try:
            remember_project(str(path))
        except OSError as exc:
            return f"could not save settings: {exc}"
        return ""

    def closeEvent(self, event) -> None:  # noqa: N802
        from panelcad.diag.crash import mark_clean_exit
        from panelcad.theme import unsubscribe

        # The welcome page hides every dock. Saving that state would replace
        # the editor layout the next project is supposed to restore.
        try:
            self._share_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.session.release_share_lock()
        except Exception:  # noqa: BLE001
            pass
        self._pause_tour_on_close()
        if not self._on_start_page():
            self._save_layout()
        self._closing = True
        self.troubleshoot.stop_explain()
        self._dock_tab_timer.stop()
        self._release_dock_tab_watchers()
        self._ai_timer.stop()
        self._check_runner.stop()
        unsubscribe(self.refresh_theme)
        # The window is freed on close (WA_DeleteOnClose). Drop the canvas's
        # item wrappers before the scenes go, so none dangles.
        self.canvas.release_scenes()
        # The session can outlive this window; do not leave it calling in.
        listeners = getattr(self.session, "_listeners", None)
        if isinstance(listeners, list):
            listeners[:] = [fn for fn in listeners if fn != self._on_ai_event]
        mark_clean_exit(force=True)
        super().closeEvent(event)

    def _save_layout(self) -> None:
        from panelcad.settings import update

        raw = bytes(self.saveState().toBase64()).decode("ascii")
        try:
            update({"layout": raw})
        except OSError:
            return

    def _restore_layout(self) -> bool:
        from PySide6.QtCore import QByteArray

        from panelcad.settings import get

        raw = get("layout")
        if not isinstance(raw, str) or not raw.strip():
            return False
        return bool(self.restoreState(QByteArray.fromBase64(raw.encode("ascii"))))

    def _on_checks(self, rows: list) -> None:
        self._checks_tick += 1
        payload = [dict(row) for row in rows]
        self.problems.set_rows(payload)
        self.canvas.set_findings(payload)
        self.power.refresh()
        counts = self.problems.counts()
        n = counts["error"] + counts["warning"] + counts["info"]
        self.problems_wrap.setWindowTitle(f"Problems ({n})" if n else "Problems")
        self._refresh_status_widgets()

    def reconstruct_from_photos(self) -> None:
        from panelcad.gui.reconstruct_wizard import ReconstructWizard

        if self._on_start_page():
            self.new_project()
        dialog = ReconstructWizard(self.session, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = dialog.apply_result
        if accepted and result is not None and result.applied and result.sheet_id:
            try:
                self.session.set_active_sheet(result.sheet_id)
            except KeyError:
                pass
        self._refresh_from_session(fit=False)
        if accepted and result is not None and result.summary:
            self._status(result.summary)
        self._check_runner.schedule()

    def _peek_reconstruct_photo(self) -> None:
        peek = getattr(self, "photo_peek", None)
        if peek is None:
            return
        ref = self._selected_ref()
        if not ref or self.session.project.reconstruct is None:
            peek.show_payload(None)
            return
        try:
            payload = self.session.reconstruct_photo_region(ref=ref)
        except KeyError:
            peek.show_payload(None)
            return
        peek.show_payload(payload)

    def _fix_finding(self, row: dict) -> None:
        """One dialog for the selected Problems row."""

        action = str(row.get("action") or "")
        check = str(row.get("check") or "")
        item_id = str(row.get("item_id") or "").strip()
        ref = str(row.get("ref") or "").strip()
        photo_id = str(row.get("photo_id") or "").strip()
        if action == "pick_catalog" and item_id:
            self._fix_reconstruct_catalog(item_id)
            return
        if action == "confirm_connection":
            self._fix_confirm_connection(row)
            return
        if ref and (check in _LAYOUT_FIX_CHECKS or action in _LAYOUT_FIX_ACTIONS):
            self._offer_layout_fix(row)
            return
        if check == "nameplate_missing_field":
            self.open_nameplate()
            return
        if photo_id or (item_id and check.startswith("reconstruct_")):
            self._jump_finding(row)
            return
        self._explain_finding(row)

    def _fix_reconstruct_catalog(self, item_id: str) -> None:
        from panelcad.gui.part_picker import pick_catalog_part

        part = pick_catalog_part(self, self.session)
        if part is None:
            return
        try:
            self.session.reconstruct_confirm_item(
                item_id,
                catalog=str(part.catalog),
                manufacturer=str(part.manufacturer),
            )
        except (KeyError, ValueError) as exc:
            self._status(str(exc))
            return
        self._check_runner.run_now()

    def _fix_confirm_connection(self, row: dict) -> None:
        answer = QMessageBox.question(
            self,
            "Confirm connection",
            str(row.get("message") or "Confirm this connection?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.session.reconstruct_confirm_connection(
                wire_id=str(row.get("ref") or row.get("item_id") or "")
            )
        except (KeyError, ValueError) as exc:
            self._status(str(exc))
            return
        self._check_runner.run_now()

    def _offer_layout_fix(self, row: dict) -> None:
        ref = str(row.get("ref") or "").strip()
        if not ref:
            self._explain_finding(row)
            return
        self._jump_finding(row, photos=False)
        box = QMessageBox(self)
        box.setWindowTitle("Fix layout part")
        box.setText(str(row.get("message") or ref))
        box.setInformativeText("Every laid-out part needs a catalog SKU or a confirmed envelope.")
        catalog = box.addButton("Catalog part…", QMessageBox.ButtonRole.AcceptRole)
        envelope = box.addButton("Confirm envelope…", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is catalog:
            self.assign_catalog_part(ref)
        elif clicked is envelope:
            self.confirm_part_envelope(ref)

    def _explain_finding(self, row: dict) -> None:
        message = str(row.get("message") or row.get("fix") or "").strip()
        check = str(row.get("check") or "")
        ref = str(row.get("ref") or "").strip() or "the part"
        extra = _FIX_NEXT_STEP.get(check, "").format(ref=ref)
        if extra and extra not in message:
            message = f"{message}\n\n{extra}" if message else extra
        if not message:
            message = "This row has no automatic fix."
        QMessageBox.information(self, "Problems", message)

    def _jump_finding(self, row: dict, *, photos: bool = True) -> None:
        photo_id = str(row.get("photo_id") or "").strip()
        item_id = str(row.get("item_id") or "").strip()
        if photos and (photo_id or (item_id and str(row.get("check") or "").startswith("reconstruct_"))):
            try:
                payload = self.session.reconstruct_photo_region(
                    item_id=item_id or None,
                    photo_id=photo_id or None,
                )
            except (KeyError, ValueError):
                payload = None
            if payload:
                from panelcad.gui.photo_viewer import PhotoViewerDialog

                PhotoViewerDialog(self.session, payload, self).exec()
        sheet_id = str(row.get("sheet") or "").strip()
        if sheet_id and sheet_id != self.session.active_sheet_id:
            try:
                self.session.set_active_sheet(sheet_id)
            except KeyError:
                pass
            else:
                self.canvas.reload()
                self.sheet_bar.refresh()
        self.canvas.set_findings(self.problems._rows)
        self.canvas.jump_to_finding(row)
        self.problems_wrap.show()
        self.problems_wrap.raise_()
        ref = str(row.get("ref") or "")
        msg = str(row.get("message") or ref)
        self._status(msg)

    def _suppress_finding(self, finding_id: str) -> None:
        try:
            self.session.suppress_finding(finding_id)
        except ValueError as exc:
            self._status(str(exc))
            return
        self._check_runner.run_now()
        self._status(f"ignored {finding_id}")

    def _unsuppress_finding(self, finding_id: str) -> None:
        self.session.unsuppress_finding(finding_id)
        self._check_runner.run_now()
        self._status(f"restored {finding_id}")

    def export_checks(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Design checks", "checks.csv", "CSV (*.csv)")
        if not path:
            return
        dest = self.session.export_checks(path, include_suppressed=True)
        self._status(f"checks {dest}")

    def export_power_budget(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Control power budget", "power-budget.pdf", "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        dest = self.session.export_power_budget_pdf(path)
        self._status(f"power budget {dest}")

    def _make_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self._act("&New", QKeySequence.StandardKey.New, self.new_project))
        file_menu.addAction(self._act("&Open…", QKeySequence.StandardKey.Open, self.open_project))
        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._recent_menu.setObjectName("recentMenu")
        # Status tip is what the guidance pass keeps. A tooltip equal to the
        # label is treated as missing and replaced with the label itself.
        recent_tip = "Reopen one of the last 10 projects"
        self._recent_menu.menuAction().setToolTip(recent_tip)
        self._recent_menu.menuAction().setStatusTip(recent_tip)
        self._recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(self._act("&Save", QKeySequence.StandardKey.Save, self.save_project))
        file_menu.addAction(self._act("Save &As…", QKeySequence.StandardKey.SaveAs, self.save_project_as))
        file_menu.addAction(self._act("Refresh from &disk", "F5", self.refresh_from_disk))
        file_menu.addAction(self._act("Project &activity…", None, self.show_project_activity))
        file_menu.addSeparator()
        file_menu.addAction(self._act("&Print…", QKeySequence.StandardKey.Print, self.print_sheet))
        file_menu.addAction(self._act("Print Package…", None, self.print_package))
        file_menu.addAction(self._act("Export package…", None, self.export_package))
        file_menu.addAction(self._act("Export JSON…", None, self.export_json))
        file_menu.addAction(self._act("Export SVG…", None, self.export_svg))
        file_menu.addAction(self._act("Export PDF…", None, self.export_pdf))
        file_menu.addAction(self._act("Export DXF…", None, self.export_dxf))
        file_menu.addAction(self._act("Export BOM…", None, self.export_bom))
        file_menu.addAction(self._act("Export BOM grouped…", None, self.export_bom_grouped))
        file_menu.addAction(self._act("Export wire list…", None, self.export_wires))
        export_menu = file_menu.addMenu("&Export")
        export_menu.addAction(self._act("Wire Markers…", None, self.export_wire_markers))
        export_menu.addAction(self._act("PLC Tags…", None, self.export_plc_tags))
        file_menu.addSeparator()
        file_menu.addAction(self._act("Import…", None, self.import_any))
        file_menu.addAction(self._act("Import &print…", None, self.import_print))
        file_menu.addAction(self._act("Reconstruct from &photos…", None, self.reconstruct_from_photos))
        file_menu.addAction(self._act("Import BOM…", None, self.import_csv))
        file_menu.addAction(self._act("Import wires…", None, self.import_wires))
        file_menu.addAction(self._act("Import library…", None, self.import_library))
        file_menu.addAction(self._act("Symbol &Builder…", None, lambda: self.edit_library_device(None)))
        file_menu.addSeparator()
        file_menu.addAction(self._act("E&xit", QKeySequence.StandardKey.Quit, self.close))

        edit = self.menuBar().addMenu("&Edit")
        self._undo_action = self._act("&Undo", QKeySequence.StandardKey.Undo, self.undo_edit)
        self._redo_action = self._act("&Redo", "Ctrl+Shift+Z", self.redo_edit)
        edit.addAction(self._undo_action)
        edit.addAction(self._redo_action)
        edit.addSeparator()
        edit.addAction(self._act("&Select", "Esc", self.canvas.set_select_mode))
        edit.addAction(self._act("&Wire", "W", self._start_wire_mode))
        edit.addAction(self._act("&Object editor…", "Ctrl+E", self.edit_selected_object))
        edit.addAction(self._act("Set &address…", "Ctrl+Shift+A", self.edit_selected_address))
        edit.addAction(self._act("Du&plicate", "Ctrl+D", self.duplicate_selected))
        edit.addAction(self._act("&Rotate 90°", "R", self.rotate_selected))
        edit.addAction(self._act("&Bigger", "]", self.bigger))
        edit.addAction(self._act("S&maller", "[", self.smaller))
        edit.addAction(self._act("&Delete selected", QKeySequence.StandardKey.Delete, self.delete_selected))
        edit.addSeparator()
        edit.addAction(self._act("&Unlock connection points", "U", lambda: self.canvas.set_selected_pins_locked(False)))
        edit.addAction(self._act("&Lock connection points", "Shift+U", lambda: self.canvas.set_selected_pins_locked(True)))
        edit.addAction(self._act("Reset connection points", "Alt+U", self.canvas.reset_selected_pins))
        edit.addSeparator()
        prefs = edit.addMenu("&Drawing prefs")
        prefs.addAction(self._act("Standard: &IEC", None, lambda: self.set_drawing_standard("IEC")))
        prefs.addAction(self._act("Standard: &NEMA/ANSI", None, lambda: self.set_drawing_standard("NEMA/ANSI")))
        prefs.addSeparator()
        prefs.addAction(self._act("Style: &ladder", None, lambda: self.set_drawing_style("ladder")))
        prefs.addAction(self._act("Style: sc&hematic", None, lambda: self.set_drawing_style("schematic")))
        prefs.addAction(self._act("Style: &freeform", None, lambda: self.set_drawing_style("freeform")))
        prefs.addAction(self._act("Choose print style…", None, self.choose_print_style))
        prefs.addSeparator()
        prefs.addAction(self._act("Coil style: &circle", "Shift+C", lambda: self.set_coil_style("circle")))
        prefs.addAction(self._act("Coil style: s&quare", "Shift+Q", lambda: self.set_coil_style("square")))
        prefs.addSeparator()
        prefs.addAction(self._act("Wire numbers: sheet/line (101…)", None, lambda: self.set_wire_numbering("sheet_line")))
        prefs.addAction(self._act("Wire numbers: sequential", None, lambda: self.set_wire_numbering("sequential")))
        prefs.addAction(self._act("Wire numbers: IEC (1-1)", None, lambda: self.set_wire_numbering("iec")))
        prefs.addAction(self._act("Auto wire numbers on", None, lambda: self.set_wire_numbering(enabled=True)))
        prefs.addAction(self._act("Auto wire numbers off", None, lambda: self.set_wire_numbering(enabled=False)))
        prefs.addSeparator()
        prefs.addAction(self._act("NC mark: underline", None, lambda: self.set_xref_nc_mark("underline")))
        prefs.addAction(self._act("NC mark: bar", None, lambda: self.set_xref_nc_mark("bar")))
        prefs.addAction(self._act("NC mark: parentheses", None, lambda: self.set_xref_nc_mark("parentheses")))
        prefs.addSeparator()
        prefs.addAction(self._act("NEMA line pitch: 0.25 in (6.35 mm)", None, lambda: self.set_xref_rung_pitch(6.35)))
        prefs.addAction(self._act("NEMA line pitch: 0.5 in (12.7 mm)", None, lambda: self.set_xref_rung_pitch(12.7)))
        prefs.addAction(self._act("NEMA line pitch: 1 in (25.4 mm)", None, lambda: self.set_xref_rung_pitch(25.4)))
        prefs.addAction(self._act("NEMA line pitch…", None, self.choose_xref_rung_pitch))
        edit.addSeparator()
        edit.addAction(self._act("&Settings…", None, self.open_settings))
        edit.addAction(self._act("Scan &vision…", None, self.open_vision_settings))

        view = self.menuBar().addMenu("&View")
        view.addAction(self._act("&Fit page", "Home", self.canvas.fit_page))
        view.addAction(self._act("Zoom to &drawing", "Ctrl+0", self.canvas.fit_drawing))
        view.addAction(self._act("&Next sheet", "PgDown", self.next_sheet))
        view.addAction(self._act("&Previous sheet", "PgUp", self.prev_sheet))
        view.addSeparator()
        view.addAction(self._dock_action(self.lib_wrap, "&Library", "Ctrl+1"))
        view.addAction(self._dock_action(self.prop_wrap, "&Inspector", "Ctrl+2"))
        view.addAction(self._dock_action(self.bom_wrap, "&BOM", "Ctrl+3"))
        view.addAction(self._dock_action(self.ai_wrap, "&AI", "Ctrl+4"))
        view.addAction(self._dock_action(self.problems_wrap, "&Problems", "Ctrl+5"))
        view.addAction(self._dock_action(self.troubleshoot_wrap, "&Troubleshoot"))
        view.addAction(self._dock_action(self.next_steps_wrap, "Next &steps", "Ctrl+8"))
        view.addAction(self._act("&Help panel", None, lambda: self.show_help("start")))
        view.addAction(self._dock_action(self.power_wrap, "P&ower budget", "Ctrl+6"))
        view.addAction(self._dock_action(self.quote_wrap, "&Quote", "Ctrl+7"))
        view.addAction(self._dock_action(self.import_review_wrap, "Print &review", "Ctrl+9"))
        view.addSeparator()
        theme_menu = view.addMenu("&Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for key, label in (("light", "&Light"), ("dark", "&Dark"), ("system", "&Match Windows")):
            act = theme_menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            theme_group.addAction(act)
            act.triggered.connect(self._theme_action_triggered)
            self._theme_actions[key] = act
        canvas_menu = view.addMenu("&Sheet")
        canvas_group = QActionGroup(self)
        canvas_group.setExclusive(True)
        for key, label in (("paper", "&Paper"), ("white", "&White"), ("dark", "&Dark"), ("auto", "&Auto")):
            act = canvas_menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            canvas_group.addAction(act)
            act.triggered.connect(self._canvas_action_triggered)
            self._canvas_actions[key] = act
        self._phase_action = QAction("Phase colors on screen", self)
        self._phase_action.setCheckable(True)
        self._phase_action.triggered.connect(self._choose_phase)
        view.addAction(self._phase_action)
        view.addAction(self._act("&Settings…", "Ctrl+,", self.open_settings))
        view.addAction(self._act("Reset &layout", None, self.reset_tool_windows))

        insert = self.menuBar().addMenu("&Insert")
        insert.addAction(self._act("&Sheet…", None, self.new_sheet))
        insert.addAction(self._act("&Panel…", None, self.new_panel))
        insert.addAction(self._act("Circuit &Builder…", None, self.insert_circuit))
        insert.addAction(self._act("PLC &I/O sheets…", None, self.insert_io_sheets))
        insert.addAction(self._act("&Terminal strip…", None, self.open_terminal_strip))
        insert.addAction(self._act("Symbol &Builder…", None, lambda: self.edit_library_device(None)))
        insert.addAction(self._act("Company &logo…", None, self.insert_logo))
        insert.addAction(self._act("Build layout from sc&hematic", None, self.pack_from_schematic))
        insert.addAction(self._act("Pack &I/O ladder", "Ctrl+Shift+I", self.pack_io_ladder))
        insert.addAction(self._act("&Cleanup sweep", "Ctrl+Shift+U", self.cleanup_sweep))
        insert.addAction(self._act("Example: DOL starter", None, self.load_example))

        tools = self.menuBar().addMenu("&Tools")
        tools.addAction(self._act("&Troubleshoot…", "Ctrl+Shift+T", self.show_troubleshoot))
        panel_menu = tools.addMenu("&Panel")
        panel_menu.addAction(self._act("&Size Enclosure…", None, self.size_enclosure))
        panel_menu.addAction(self._act("&Thermal…", None, self.thermal_report))
        tools.addAction(self._act("&Motor sizing", None, self.show_motor_sizing))
        panel_menu.addAction(self._act("Cutout &drawings…", None, self.cutout_drawings))
        tools.addAction(self._act("Reconstruct from &photos…", None, self.reconstruct_from_photos))
        tools.addAction(self._act("Build layout from sc&hematic", None, self.pack_from_schematic))
        tools.addAction(self._act("Convert drawing standard...", None, self.convert_drawing_standard))

        strip_menu = self.menuBar().addMenu("Stri&p")
        strip_menu.addAction(self._act("&Open strip…", None, self.open_terminal_strip))
        strip_menu.addSeparator()
        strip_menu.addAction(self._act("Add &terminal (2.5 mm²)", None, lambda: self.add_strip_terminal("iec.terminals.x_2p5")))
        strip_menu.addAction(self._act("Add &PE terminal", None, lambda: self.add_strip_terminal("iec.terminals.pe_4")))
        strip_menu.addAction(self._act("Add &partition", None, self.add_strip_partition))
        strip_menu.addAction(self._act("&Jumper selected", None, self.jumper_selected_terminals))
        strip_menu.addAction(self._act("Remove &jumper", None, self.clear_selected_jumpers))
        strip_menu.addAction(self._act("Move &earlier", None, lambda: self.move_selected_terminal("earlier")))
        strip_menu.addAction(self._act("Move &later", None, lambda: self.move_selected_terminal("later")))
        strip_menu.addAction(self._act("&Renumber tags", None, self.retag_open_strip))

        sheet_menu = self.menuBar().addMenu("&Sheets")
        sheet_menu.addAction(self._act("&New sheet…", "Ctrl+Shift+N", self.new_sheet))
        sheet_menu.addAction(self._act("&Rename sheet…", "F2", self.rename_sheet))
        sheet_menu.addAction(self._act("&Close page", "Ctrl+W", self.close_sheet))
        sheet_menu.addAction(self._act("&Delete page…", "Ctrl+Shift+W", self.delete_sheet))
        self._closed_menu = sheet_menu.addMenu("Closed pages")
        sheet_menu.aboutToShow.connect(self._refresh_closed_menu)
        sheet_menu.addAction(self._act("Build layout from schematic", None, self.pack_from_schematic))
        sheet_menu.addAction(self._act("Pack I/O ladder", None, self.pack_io_ladder))
        sheet_menu.addAction(self._act("Cleanup sweep", None, self.cleanup_sweep))

        reports = self.menuBar().addMenu("&Reports")
        reports.addAction(self._act("&Number wires", None, self.number_wires))
        reports.addAction(self._act("Update &cross-references", None, self.update_cross_references))
        reports.addAction(self._act("Assign &item numbers", None, self.assign_item_numbers))
        reports.addSeparator()
        reports.addAction(self._act("Export &wire list…", None, self.export_wires))
        reports.addAction(self._act("Wire List (From-&To)…", None, self.export_from_to))
        reports.addAction(self._act("Net wire defaults…", None, self.edit_net_wire_defaults))
        reports.addAction(self._act("Export cross-&reference…", None, lambda: self._export_report("Cross-reference", self.session.export_cross_reference)))
        reports.addAction(self._act("Export &terminal strips…", None, lambda: self._export_report("Terminal strips", self.session.export_terminal_strips)))
        reports.addAction(self._act("Export Phoenix strip (PROJECT complete)…", None, lambda: self._export_vendor_strip("phoenix")))
        reports.addAction(self._act("Export Phoenix CLIPX XML…", None, lambda: self._export_vendor_strip("clipx", "xml")))
        reports.addAction(self._act("Export WAGO strip (smartDESIGNER)…", None, lambda: self._export_vendor_strip("wago")))
        reports.addAction(self._act("Export WAGO smartDESIGNER XML…", None, lambda: self._export_vendor_strip("wagoxml", "xml")))
        reports.addAction(self._act("Export Weidmüller strip (WMC)…", None, lambda: self._export_vendor_strip("weidmueller")))
        reports.addAction(self._act("Schematic / &panel audit…", None, lambda: self._export_report("Schematic panel audit", self.session.export_audit)))
        reports.addAction(self._act("Design &checks…", None, self.export_checks))
        reports.addAction(self._act("&Nameplate…", None, self.open_nameplate))
        reports.addAction(self._act("Control &power budget…", None, self.export_power_budget))
        reports.addAction(self._act("PLC &I/O List…", None, lambda: self._export_report("PLC I/O List", self.session.export_io_list)))
        reports.addAction(self._act("Short-circuit rating (&SCCR)…", None, self.open_sccr))
        reports.addAction(self._act("Import PLC I/O…", None, self.import_io))
        reports.addAction(self._act("Regenerate I/O sheets", None, self.regenerate_io_sheets))
        reports.addAction(self._act("Field &cables…", None, self.edit_cables))
        reports.addAction(self._act("Export &cable schedule…", None, lambda: self._export_report("Cable schedule", self.session.export_cable_schedule)))
        reports.addAction(self._act("Export cable &pull list…", None, lambda: self._export_report("Cable pull list", self.session.export_cable_pull_list)))
        reports.addAction(self._act("Export cable &terminations…", None, lambda: self._export_report("Cable terminations", self.session.export_cable_terminations)))
        reports.addAction(self._act("Panel wire &annotation…", None, lambda: self._export_report("Panel wire annotation", self.session.export_panel_wires)))
        reports.addSeparator()
        reports.addAction(self._act("&Quote", None, self.show_quote))

        revisions = self.menuBar().addMenu("&Revisions")
        revisions.addAction(self._act("&Issue revision…", None, self.issue_revision))
        revisions.addAction(self._act("Revision &history…", None, self.show_revision_history))
        revisions.addAction(self._act("&Compare revisions…", None, self.compare_revisions))
        revisions.addSeparator()
        revisions.addAction(self._act("Revision &cloud", "C", self.canvas.set_cloud_mode))
        revisions.addAction(self._act("Clear &clouds", None, self.clear_revision_clouds))

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self._act("&Help", "F1", lambda: self.show_help("start")))
        help_menu.addAction(self._act("&Welcome", None, self.show_welcome))
        help_menu.addAction(self._act("Take the &tour", None, lambda: self.start_tour(True)))
        help_menu.addAction(self._act("Resume &tour", None, self.resume_tour))
        help_menu.addAction(self._act("Task &checklists", None, self.show_checklists))
        help_menu.addAction(self._act("&What's This?", "Shift+F1", self.enter_whats_this))
        help_menu.addSeparator()
        help_menu.addAction(self._act("&Help topics…", None, lambda: self.show_help("start")))
        help_menu.addAction(self._act("&Report a bug…", None, self.report_a_bug))
        help_menu.addAction(self._act("&Keyboard shortcuts", None, self.show_shortcuts))
        help_menu.addAction(self._act("U&pdates…", None, self.show_update_settings))
        help_menu.addAction(self._act("Check for &updates…", None, self.check_for_updates))
        help_menu.addAction(
            self._act("&Roll back to previous version…", None, self.roll_back_update)
        )
        help_menu.addAction(self._act("About", None, self.about))

    def _act(self, text: str, shortcut, slot) -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(shortcut)
        act.triggered.connect(slot)
        return act

    def _dock_action(self, dock: QDockWidget, text: str, shortcut: str | None = None) -> QAction:
        act = dock.toggleViewAction()
        act.setText(text)
        act.setShortcut(shortcut or "")
        act.setStatusTip(f"Show or hide the {dock.windowTitle()} window")
        return act

    def reset_tool_windows(self) -> None:
        """Put Library, Inspector, BOM, Problems, and AI back in their default docks."""
        self.lib_wrap.setFloating(False)
        self.prop_wrap.setFloating(False)
        self.bom_wrap.setFloating(False)
        self.quote_wrap.setFloating(False)
        self.problems_wrap.setFloating(False)
        self.power_wrap.setFloating(False)
        self.sizing_wrap.setFloating(False)
        self.photo_wrap.setFloating(False)
        self.import_review_wrap.setFloating(False)
        self.ai_wrap.setFloating(False)
        self.next_steps_wrap.setFloating(False)
        self.troubleshoot_wrap.setFloating(False)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.lib_wrap)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.prop_wrap)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_wrap)
        self.tabifyDockWidget(self.prop_wrap, self.ai_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.bom_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.problems_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.power_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.sizing_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.photo_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.quote_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.import_review_wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.troubleshoot_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.problems_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.photo_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.power_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.sizing_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.quote_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.next_steps_wrap)
        self.tabifyDockWidget(self.bom_wrap, self.troubleshoot_wrap)
        self.tabifyDockWidget(self.problems_wrap, self.import_review_wrap)
        self.lib_wrap.show()
        self.prop_wrap.show()
        self.bom_wrap.show()
        self.quote_wrap.show()
        self.problems_wrap.show()
        self.power_wrap.show()
        self.sizing_wrap.show()
        self.photo_wrap.show()
        self.ai_wrap.show()
        self.next_steps_wrap.show()
        self.troubleshoot_wrap.show()
        self.bom_wrap.raise_()
        self.ai_wrap.raise_()
        self.bom_wrap.raise_()
        self.toolbar.show()
        self._apply_default_docks()
        self._hide_print_review_until_import()
        self._save_layout()
        self._layout_applied = True
        self._hide_duplicate_dock_tabs()
        self._status(
            "Library, Inspector, BOM, Problems, Troubleshoot, Power, "
            "Quote, Next steps, Print review, and AI restored"
        )

    def restoreState(self, state, version: int = 0) -> bool:  # noqa: N802
        restored = super().restoreState(state, version)
        # A saved layout remembers sizes. It must not put the minimums back.
        self._unlock_tool_docks()
        self._hide_duplicate_dock_tabs()
        self._schedule_dock_tab_hide()
        return restored

    def _named_tool_docks(self) -> list[QDockWidget]:
        """Editor docks by attribute. This does not walk a dialog's children."""

        docks: list[QDockWidget] = []
        for name in (
            "lib_wrap",
            "prop_wrap",
            "ai_wrap",
            "bom_wrap",
            "problems_wrap",
            "power_wrap",
            "quote_wrap",
            "sizing_wrap",
            "photo_wrap",
            "next_steps_wrap",
            "troubleshoot_wrap",
            "import_review_wrap",
        ):
            dock = getattr(self, name, None)
            if isinstance(dock, QDockWidget) and _qt_alive(dock):
                docks.append(dock)
        return docks

    def _owns_dock_tab_bar(self, bar: QTabBar) -> bool:
        """True when this window owns the strip. A dialog's tab bar is not one."""

        if not _qt_alive(bar):
            return False
        try:
            parent = bar.parent()
        except RuntimeError:
            return False
        if not _qt_alive(parent) or isinstance(parent, QDialog):
            return False
        return parent is self

    def _dock_area_tab_bars(self) -> list[QTabBar]:
        """Dock strips parented on this window. Sheet and quote tabs are deeper."""

        try:
            children = list(self.children())
        except RuntimeError:
            return []
        bars: list[QTabBar] = []
        for child in children:
            if not _qt_alive(child) or not isinstance(child, QTabBar):
                continue
            if self._owns_dock_tab_bar(child):
                bars.append(child)
        return bars

    def _dock_tab_rank(self, bar: QTabBar) -> tuple[int, int, int, int, int, int]:
        """Prefer the bar on the dock edge. A restore leftover covers the dock or sits off-window.

        resizeDocks shows a second bar at the new dock width and leaves the old
        one in place. Hiding the bar that lines up with the on-screen dock makes
        Qt show it again, so that bar wins a tie. A background tab can report a
        stale rectangle outside the window; that rectangle does not count.
        """

        worst = (1, 1, 0, 0, 0, 0)
        if not _qt_alive(bar):
            return worst
        try:
            origin = bar.mapTo(self, bar.rect().topLeft())
            rect = bar.rect().translated(origin)
            outside = 0 if self.rect().intersects(rect) else 1
            titles = _tab_titles(bar)
            aligned = 1
            matched = 0
            overlap = 0
            for dock in self._named_tool_docks():
                try:
                    if not dock.isVisible() or dock.isFloating():
                        continue
                    body_origin = dock.mapTo(self, dock.rect().topLeft())
                    body = dock.rect().translated(body_origin)
                    # Tabified docks that are not current keep a geometry outside
                    # the window. Matching that rectangle would keep the orphan.
                    if not self.rect().intersects(body):
                        continue
                    if _dock_title(dock) in titles:
                        same_x = abs(body_origin.x() - origin.x()) <= 3
                        same_w = abs(body.width() - rect.width()) <= 3
                        if same_x and same_w:
                            aligned = 0
                            matched = max(matched, body.width() * body.height())
                    hit = rect.intersected(body)
                    if not hit.isEmpty():
                        overlap += hit.width() * hit.height()
                except RuntimeError:
                    continue
            return (outside, aligned, -matched, overlap, -rect.bottom(), -rect.right())
        except RuntimeError:
            return worst

    def _bottom_docks(self) -> list[QDockWidget]:
        docks: list[QDockWidget] = []
        for name in (
            "bom_wrap",
            "problems_wrap",
            "photo_wrap",
            "power_wrap",
            "sizing_wrap",
            "quote_wrap",
            "next_steps_wrap",
            "troubleshoot_wrap",
            "import_review_wrap",
        ):
            dock = getattr(self, name, None)
            if isinstance(dock, QDockWidget) and _qt_alive(dock):
                docks.append(dock)
        return docks

    def _hide_print_review_until_import(self) -> None:
        """Keep Print review off the sheet until Import print has something to show."""

        if getattr(self, "_import_job", None) is not None:
            return
        wrap = getattr(self, "import_review_wrap", None)
        if wrap is not None:
            wrap.hide()

    def _unlock_tool_docks(self) -> None:
        """Drop min/max locks so the library and bottom handles can move."""

        floor = _ToolDock.FLOOR
        for dock in self._named_tool_docks():
            dock.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            dock.setMinimumSize(floor)
            dock.setMaximumSize(16777215, 16777215)
            widget = dock.widget()
            if widget is not None:
                _release_dock_contents(widget)

    def _apply_default_docks(self) -> None:
        """Canvas-first sizes. The bottom docks stay tabbed and short."""

        wide = self.width() >= NARROW_WINDOW_PX
        self._unlock_tool_docks()
        self.resizeDocks(
            [self.lib_wrap],
            [WIDE_LIBRARY_PX if wide else 220],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks([self.prop_wrap], [260 if wide else 180], Qt.Orientation.Horizontal)
        if self.bom_wrap.isVisible():
            # One height for the shared bottom tab. Listing every tab splits them.
            self.resizeDocks([self.bom_wrap], [120], Qt.Orientation.Vertical)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "lib_wrap", None) is None:
            return
        self._apply_screen_docks()

    def _apply_screen_docks(self) -> None:
        """Under 1280 px, side docks share one tab column and the sheet keeps its height."""

        if self._closing or self._applying_docks or self._on_start_page():
            return
        narrow = self.width() < NARROW_WINDOW_PX
        if narrow == self._narrow_docks_on:
            if not narrow:
                self._use_wide_library()
            return
        self._applying_docks = True
        try:
            self._narrow_docks_on = narrow
            if narrow:
                self._collapse_for_narrow_screen()
            else:
                self._expand_after_narrow_screen()
        finally:
            self._applying_docks = False

    def _fit_narrow_canvas(self) -> None:
        # A fixed floor. Tying this to the current height repaints forever.
        self._editor.setMinimumHeight(240)

    def _collapse_for_narrow_screen(self) -> None:
        """One side column of tabs, and a short bottom strip, so the sheet stays usable."""

        # Library stays on the left. The tour card sits to its right.
        # Inspector and AI already share the right-hand tab column.
        # Do not show a dock the saved layout hid.
        if self.prop_wrap.isVisible() and self.ai_wrap.isVisible():
            self.tabifyDockWidget(self.prop_wrap, self.ai_wrap)
        self._place_help_dock(narrow=True)
        self._unlock_tool_docks()
        if self.lib_wrap.isVisible():
            self.resizeDocks([self.lib_wrap], [200], Qt.Orientation.Horizontal)
        if self.prop_wrap.isVisible():
            self.resizeDocks([self.prop_wrap], [180], Qt.Orientation.Horizontal)
            self.prop_wrap.raise_()
        self._wrap_bottom_docks()
        # One height for the shared bottom tab group. Listing every tab
        # makes Qt split them into a stack and eat the sheet.
        if self.bom_wrap.isVisible():
            # resizeDocks asks for the area. The dock widget comes out about
            # 30 px shorter, so 200 lands near 170: tall enough for the Fix
            # button and the next-steps list, short enough that the sheet stays.
            self.resizeDocks([self.bom_wrap], [200], Qt.Orientation.Vertical)
            self.bom_wrap.raise_()
        self._fit_narrow_canvas()
        self._hide_duplicate_dock_tabs()

    def _wrap_bottom_docks(self) -> None:
        """Let a short bottom strip scroll the dock at its natural size."""

        from PySide6.QtWidgets import QFrame, QScrollArea

        for dock in self._bottom_docks():
            widget = dock.widget()
            if widget is None or widget.objectName() == "narrowDockScroll":
                continue
            scroll = QScrollArea(dock)
            scroll.setObjectName("narrowDockScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setMinimumHeight(100)
            dock.setWidget(scroll)
            scroll.setWidget(widget)

    def _unwrap_bottom_docks(self) -> None:
        for dock in self._bottom_docks():
            scroll = dock.widget()
            if scroll is None or scroll.objectName() != "narrowDockScroll":
                continue
            inner = scroll.takeWidget()
            if inner is not None:
                dock.setWidget(inner)
            scroll.deleteLater()

    def _expand_after_narrow_screen(self) -> None:
        # Remember what the user left open. Moving a dock can show it again.
        side_open = {
            self.lib_wrap: self.lib_wrap.isVisible(),
            self.prop_wrap: self.prop_wrap.isVisible(),
            self.ai_wrap: self.ai_wrap.isVisible(),
        }
        self._editor.setMinimumHeight(0)
        self._unwrap_bottom_docks()
        self._place_help_dock(narrow=False)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.lib_wrap)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.prop_wrap)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_wrap)
        self.tabifyDockWidget(self.prop_wrap, self.ai_wrap)
        for dock, opened in side_open.items():
            dock.setVisible(opened)
        if side_open[self.ai_wrap]:
            self.ai_wrap.raise_()
        elif side_open[self.prop_wrap]:
            self.prop_wrap.raise_()
        self._unlock_tool_docks()
        self._apply_default_docks()
        self._hide_duplicate_dock_tabs()

    def _use_wide_library(self) -> None:
        """Keep a wide window from pinning the library, and recover a collapsed one."""

        if self._applying_docks or self.width() < NARROW_WINDOW_PX:
            return
        if not self.lib_wrap.isVisible():
            return
        # The old 360 px minimum is what made the left handle feel welded.
        # A saved or dragged width stays. Only a collapsed strip is opened.
        if self.lib_wrap.width() >= _ToolDock.FLOOR.width():
            return
        self._applying_docks = True
        try:
            self._unlock_tool_docks()
            self.resizeDocks([self.lib_wrap], [WIDE_LIBRARY_PX], Qt.Orientation.Horizontal)
        finally:
            self._applying_docks = False

    def childEvent(self, event) -> None:  # noqa: N802
        super().childEvent(event)
        if getattr(self, "_closing", True):
            return
        child = event.child()
        if event.added() and _qt_alive(child) and isinstance(child, QTabBar):
            self._watch_dock_tab_bar(child)

    def event(self, event) -> bool:  # noqa: N802
        layout = (
            event.type() == QEvent.Type.LayoutRequest
            and self.isVisible()
            and not getattr(self, "_closing", True)
        )
        if layout:
            self._watch_dock_tab_bars()
        result = super().event(event)
        # resizeDocks can create the leftover bar inside this layout. Watch it
        # so the show that follows is observed. Do not hide from in here.
        if layout:
            self._watch_dock_tab_bars()
        return result

    def _release_dock_tab_watchers(self) -> None:
        """Drop tab-bar filters on close so a hidden window cannot hide/show forever."""

        for bar in self._dock_area_tab_bars():
            try:
                bar.removeEventFilter(self)
                bar.setProperty("_pc_dock_tabs", False)
            except RuntimeError:
                continue

    def _watch_dock_tab_bars(self) -> None:
        if self._closing or not self.isVisible():
            return
        for bar in self._dock_area_tab_bars():
            self._watch_dock_tab_bar(bar)

    def _watch_dock_tab_bar(self, bar: QTabBar) -> None:
        if self._closing or not self._owns_dock_tab_bar(bar):
            return
        try:
            if bar.property("_pc_dock_tabs"):
                return
            bar.setProperty("_pc_dock_tabs", True)
            bar.setElideMode(Qt.TextElideMode.ElideNone)
            bar.setUsesScrollButtons(True)
            bar.setExpanding(False)
            bar.installEventFilter(self)
            visible = bar.isVisible()
        except RuntimeError:
            return
        # The show can happen in the layout that created the bar, before this
        # filter is installed. Schedule the hide; do not hide inside the layout
        # and do not walk other bars from this event.
        if visible:
            self._schedule_dock_tab_hide()

    def _tab_bar_is_stale_subset(self, bar: QTabBar, titles: set[str]) -> bool:
        """A leftover strip lists docks that are all tabified into a bigger group.

        One dock is not enough. During a split the first dock can still report
        the old group while another dock already matches this strip. A live
        split stays visible. A real leftover, whose docks all sit in the bigger
        group, still hides.
        """

        named = [dock for dock in self._named_tool_docks() if _dock_title(dock) in titles]
        if not named:
            return True
        for dock in named:
            title = _dock_title(dock)
            if not title:
                return False
            group = {title}
            try:
                others = self.tabifiedDockWidgets(dock)
            except RuntimeError:
                return False
            for other in others:
                other_title = _dock_title(other)
                if other_title:
                    group.add(other_title)
            if not titles < group:
                return False
        return True

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Queue a dock-tab cleanup. Do not walk or hide from inside the event.

        Help > Check for updates runs a modal progress dialog. Accepting it
        relayouts the docks and can destroy a tab bar while this filter is
        still on the stack. A tree walk from here aborts the process. One
        coalesced timer runs after the event returns, and only on strips this
        window owns. A closed window stays out: its bars are what the next
        style change walks.
        """

        if getattr(self, "_closing", True) or not self.isVisible():
            return False
        if event.type() not in {QEvent.Type.Show, QEvent.Type.ShowToParent, QEvent.Type.Resize}:
            return False
        if not _qt_alive(watched) or not isinstance(watched, QTabBar):
            return False
        if not self._owns_dock_tab_bar(watched):
            return False
        if self._hiding_dock_tabs:
            self._hide_dock_tabs_again = True
        self._schedule_dock_tab_hide()
        return False

    def _schedule_dock_tab_hide(self) -> None:
        if getattr(self, "_closing", True) or not self.isVisible():
            return
        if self._dock_tab_timer.isActive():
            return
        self._dock_tab_timer.start(0)

    def _hide_duplicate_dock_tabs_deferred(self) -> None:
        if getattr(self, "_closing", True) or not self.isVisible():
            return
        self._hide_duplicate_dock_tabs()

    def _hide_duplicate_dock_tabs(self) -> None:
        """Hide a stale dock tab bar left behind when another dock joins the group.

        Tabifying Power onto BOM|Problems leaves the earlier bar on top of the
        dock contents. That bar's titles are a strict subset of the live bar.
        Hide it. Titles come from every dock, including Quote, so a bar that
        gained a new dock is still recognized. The sheet tab bar is not a
        dock bar. QMainWindow still owns the bar; reparenting or deleting it
        crashes the next QApplication.setStyle in QTabBar::tabData.

        saveState/restoreState can also leave two bars with the same titles.
        A strict-subset check keeps both. Keep the one on the dock edge.
        """

        if not self.isVisible():
            return
        if self._hiding_dock_tabs:
            # The resize shows the live bar from inside this hide. Run once more
            # after that show, instead of dropping it.
            self._hide_dock_tabs_again = True
            return
        self._hiding_dock_tabs = True
        try:
            for _ in range(4):
                self._hide_dock_tabs_again = False
                self._hide_duplicate_dock_tabs_now()
                if not self._hide_dock_tabs_again:
                    break
        finally:
            self._hiding_dock_tabs = False

    def _hide_duplicate_dock_tabs_now(self) -> None:
        self._watch_dock_tab_bars()
        dock_titles = {_dock_title(dock) for dock in self._named_tool_docks()}
        dock_titles.discard("")
        groups: list[tuple[QTabBar, set[str]]] = []
        stale: list[QTabBar] = []
        for bar in self._dock_area_tab_bars():
            titles = _tab_titles(bar)
            if not titles or not titles <= dock_titles:
                continue
            try:
                visible = bar.isVisible()
            except RuntimeError:
                continue
            if visible and len(titles) >= 2:
                groups.append((bar, titles))
            elif not visible:
                stale.append(bar)
        for bar, titles in groups:
            if any(titles < other for other_bar, other in groups if other_bar is not bar):
                # Titles can linger on the old strip after a dock is split off.
                # Hide this bar only when its docks are actually in a bigger group.
                if self._tab_bar_is_stale_subset(bar, titles):
                    stale.append(bar)
        kept: dict[frozenset[str], QTabBar] = {}
        for bar, titles in groups:
            if bar in stale or not _qt_alive(bar):
                continue
            try:
                if not bar.isVisible():
                    continue
            except RuntimeError:
                continue
            key = frozenset(titles)
            prior = kept.get(key)
            if prior is None:
                kept[key] = bar
                continue
            # Equal bars: keep the one just shown. Hiding that one makes Qt
            # show it again and leaves the older strip up.
            drop = bar if self._dock_tab_rank(bar) > self._dock_tab_rank(prior) else prior
            stale.append(drop)
            if drop is prior:
                kept[key] = bar
        for bar in stale:
            # hide() on a bar that is already hidden posts another style change.
            if not _qt_alive(bar):
                continue
            try:
                if not bar.isHidden():
                    bar.hide()
            except RuntimeError:
                continue

    def undo_edit(self) -> None:
        self.canvas.drop_open_gesture()
        if not self.session.undo():
            self._status("nothing to undo")
            return
        self._refresh_from_session(fit=False)
        self._status("undo")

    def redo_edit(self) -> None:
        self.canvas.drop_open_gesture()
        if not self.session.redo():
            self._status("nothing to redo")
            return
        self._refresh_from_session(fit=False)
        self._status("redo")

    def bigger(self) -> None:
        self.canvas.nudge_scale(1.1)

    def smaller(self) -> None:
        self.canvas.nudge_scale(1 / 1.1)

    def duplicate_selected(self) -> None:
        self.canvas.duplicate_selected()
        self.bom.refresh()
        self.props.show_project(self.session)

    def rotate_selected(self) -> None:
        self.canvas.rotate_selected()

    def edit_selected_address(self) -> None:
        items = [i for i in self.canvas.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not items:
            QMessageBox.information(self, "Set address", "Select a device first.")
            return
        self.edit_address(items[0].component_id)

    def _component_visuals(self) -> None:
        from panelcad.library.ports import effective_ports, network_node_caption

        by_id = {c.id: c for c in self.session.sheet().components}
        for item in self.canvas.scene.items():
            if isinstance(item, ComponentItem) and item.component_id in by_id:
                comp = by_id[item.component_id]
                item.apply_scale(comp.visual_scale())
                item._addr = network_node_caption(comp, effective_ports(item.device, comp))
                item.update()
        self.canvas._apply_pin_mode()

    def _title_block_changed(self) -> None:
        # Phase-color OK used to refresh the title block and leave the old
        # wire pens on the canvas until the next sheet switch.
        self.canvas.reload()
        self.sheet_bar.refresh()
        self.setWindowTitle(self._title())
        self._sync_ai_page()

    def _switch_sheet(self, sheet_id: str) -> None:
        if sheet_id == self.session.active_sheet_id:
            return
        self.canvas.settle_before_sheet_change()
        self.canvas.sync_positions()
        self.session.set_active_sheet(sheet_id)
        # Reload redraws the sheet. Next steps must not recompute on that redraw.
        self._switching_sheet = True
        try:
            self.canvas.reload()
        finally:
            self._switching_sheet = False
        self.canvas.fit_page()
        self.props.show_project(self.session)
        self.bom.refresh()
        self._sync_empty_states()
        self._refresh_command_state()
        self._status(f"sheet {self.session.sheet_index() + 1} / {len(self.session.project.sheets)}  {self.session.sheet().title}")

    def new_panel(self) -> None:
        from panelcad.gui.panel_dialog import NewPanelDialog

        dialog = NewPanelDialog(self.session, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            result = self.session.add_panel(
                values["tag"],
                name=values["name"],
                enclosure_id=values["enclosure_id"],
                system=values["system"],
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Panel", str(exc))
            return
        self.sheet_bar.refresh()
        sheet_id = str(result.get("sheet") or "")
        if sheet_id:
            self._switch_sheet(sheet_id)
        self._status(f"panel {result.get('tag')}")

    def _choose_active_panel(self, panel_id: str) -> None:
        before = self.session.active_sheet_id
        try:
            result = self.session.set_active_panel(panel_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Panel", str(exc))
            self.sheet_bar.refresh()
            return
        active = str(result.get("active_sheet") or before)
        if active != before:
            self._switch_sheet(active)
        else:
            self.sheet_bar.refresh()

    def new_sheet(self) -> None:
        kind_labels = [
            ("Schematic", "schematic"),
            ("Panel layout", "panel_layout"),
            ("I/O ladder", "io"),
            ("Communications / network", "network"),
            ("Door layout", "door_layout"),
            ("Terminal strip", "terminal"),
        ]
        label, ok = QInputDialog.getItem(
            self, "New sheet", "Page type:", [k[0] for k in kind_labels], 0, False
        )
        if not ok:
            return
        kind = next(k for name, k in kind_labels if name == label)
        if kind == "terminal":
            self.open_terminal_strip()
            return
        n = len(self.session.project.sheets) + 1
        default_title = f"Sheet {n}"
        medium = None
        if kind == "network":
            medium, ok = QInputDialog.getItem(
                self,
                "New sheet",
                "Network:",
                ["ethernet", "serial", "usb", "mixed"],
                0,
                False,
            )
            if not ok:
                return
            default_title = {
                "ethernet": "Ethernet",
                "serial": "RS-485",
                "usb": "USB",
                "mixed": "Communications",
            }.get(medium, "Communications")
        title, ok = QInputDialog.getText(self, "New sheet", "Title:", text=default_title)
        if not ok:
            return
        title = title.strip() or default_title
        from panelcad.api.session import _slug

        sid = _slug(title)
        n_id = 2
        while any(s.id == sid for s in self.session.project.sheets):
            sid = f"{_slug(title)}-{n_id}"
            n_id += 1
        self.session.begin_edit("add sheet")
        self.canvas.sync_positions()
        try:
            try:
                self.session.add_sheet(sid, title=title, kind=kind)  # type: ignore[arg-type]
                if kind == "network":
                    self.session.set_network(name=title, medium=medium)
                    self.session.layout_network(medium=medium)
                if kind == "io":
                    self.session.layout_io()
            except ValueError as exc:
                QMessageBox.warning(self, "New sheet", str(exc))
                return
        finally:
            self.session.end_edit()
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self._status(f"added sheet {title}")

    def rename_sheet(self) -> None:
        sheet = self.session.sheet()
        title, ok = QInputDialog.getText(self, "Rename sheet", "Title:", text=sheet.title)
        if not ok:
            return
        self.session.set_sheet_title(title.strip() or sheet.id)
        self.canvas.refresh_frame()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self._sync_ai_page()

    def close_sheet(self) -> None:
        self.close_sheet_id(self.session.active_sheet_id)

    def close_sheet_id(self, sheet_id: str) -> None:
        try:
            sheet = self.session.project.sheet(sheet_id)
        except KeyError:
            return
        title = sheet.title or sheet.id
        if sheet_id == self.session.active_sheet_id:
            self.canvas.sync_positions()
        try:
            self.session.close_sheet(sheet_id)
        except ValueError as exc:
            QMessageBox.information(self, "Close page", str(exc).capitalize() + ".")
            return
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self._status(f"closed page {title} — Closed button to bring it back")

    def reopen_sheet_id(self, sheet_id: str) -> None:
        try:
            self.session.open_sheet(sheet_id)
        except KeyError:
            return
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self.bom.refresh()
        self._status(f"reopened {self.session.sheet().title}")

    def _refresh_closed_menu(self) -> None:
        self._closed_menu.clear()
        closed = self.session.closed_sheets()
        if not closed:
            from panelcad.gui.guidance import explain_disabled

            act = self._closed_menu.addAction("(none closed)")
            explain_disabled(
                act,
                "Reopen is off: no pages are closed. Close a tab with X to park a page, then reopen it here.",
            )
            return
        for sheet in closed:
            i = self.session.project.sheets.index(sheet) + 1
            act = self._closed_menu.addAction(f"{i}  {sheet.title or sheet.id}")
            act.setData(sheet.id)
            act.triggered.connect(self._reopen_sheet_action_triggered)

    def move_sheet_id(self, sheet_id: str, direction: str) -> None:
        try:
            result = self.session.move_sheet(sheet_id, direction=direction)
        except (KeyError, ValueError) as exc:
            self._status(str(exc))
            return
        self.sheet_bar.refresh()
        self._sync_ai_page()
        if result.get("moved"):
            self._status(f"moved {result['id']} to sheet {int(result['index']) + 1}")
        else:
            self._status(f"{result['id']} is already at that end")

    def reorder_open_sheets(self, ids: list) -> None:
        try:
            self.session.reorder_sheets([str(item) for item in ids])
        except (KeyError, ValueError) as exc:
            self._status(str(exc))
            self.sheet_bar.refresh()
            return
        self.sheet_bar.refresh()
        self._sync_ai_page()
        self._status("reordered sheets")

    def delete_sheet(self) -> None:
        self.delete_sheet_id(self.session.active_sheet_id)

    def delete_sheet_id(self, sheet_id: str) -> None:
        if len(self.session.project.sheets) <= 1:
            QMessageBox.information(self, "Delete page", "A project must keep at least one page.")
            return
        try:
            sheet = self.session.project.sheet(sheet_id)
        except KeyError:
            return
        title = sheet.title or sheet.id
        n = len(sheet.components)
        detail = f"Delete page “{title}” ({sheet.kind}"
        detail += f", {n} device{'s' if n != 1 else ''}" if n else ""
        detail += ")?\n\nThis cannot be undone."
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete page")
        box.setText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if sheet_id == self.session.active_sheet_id:
            self.canvas.sync_positions()
        self.session.delete_sheet(sheet_id)
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self.bom.refresh()
        self._status(f"deleted page {title}")

    def rename_sheet_id(self, sheet_id: str) -> None:
        try:
            self.session.set_active_sheet(sheet_id)
        except KeyError:
            return
        self.rename_sheet()

    def next_sheet(self) -> None:
        ids = [s.id for s in self.session.open_sheets()]
        if not ids:
            return
        current = self.session.active_sheet_id
        i = ids.index(current) if current in ids else -1
        self._switch_sheet(ids[(i + 1) % len(ids)])
        self.sheet_bar.refresh()

    def prev_sheet(self) -> None:
        ids = [s.id for s in self.session.open_sheets()]
        if not ids:
            return
        current = self.session.active_sheet_id
        i = ids.index(current) if current in ids else 0
        self._switch_sheet(ids[(i - 1) % len(ids)])
        self.sheet_bar.refresh()

    def edit_selected_object(self) -> None:
        items = [i for i in self.canvas.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not items:
            QMessageBox.information(self, "Object editor", "Select a device first.")
            return
        self.edit_object(items[0].component_id)

    def edit_wire_label(self, wire_id: str) -> None:
        try:
            wire = self.session.find_wire(wire_id=wire_id)
        except (KeyError, ValueError):
            return
        text, ok = QInputDialog.getText(
            self,
            "Wire label",
            f"{wire.frm}  →  {wire.to}",
            text=wire.caption(),
        )
        if not ok:
            return
        self.session.set_wire_label(wire_id, label=text)
        self.canvas._redraw_wires()
        self.bom.refresh()
        self._status(f"{wire.id}  {text.strip() or '(no label)'}")

    def edit_wire_number(self, wire_id: str) -> None:
        try:
            wire = self.session.find_wire(wire_id=wire_id)
        except (KeyError, ValueError):
            return
        text, ok = QInputDialog.getText(
            self,
            "Wire number",
            f"{wire.frm}  →  {wire.to}  (locks this number)",
            text=wire.number or wire.caption(),
        )
        if not ok:
            return
        self.session.set_wire_spec(wire_id, number=text.strip(), locked=bool(text.strip()))
        self.canvas._redraw_wires()
        self.bom.refresh()
        self._status(f"{wire.id}  number {text.strip() or '(cleared)'}")

    def set_wire_numbering(self, scheme: str | None = None, enabled: bool | None = None) -> None:
        settings = self.session.set_wire_numbering(scheme=scheme, enabled=enabled)
        self.props.show_project(self.session)
        state = "on" if settings["enabled"] else "off"
        self._status(f"wire numbers  {settings['scheme']}  auto {state}")

    def set_xref_nc_mark(self, nc_mark: str) -> None:
        settings = self.session.set_cross_reference(nc_mark=nc_mark)
        self.canvas.reload()
        self.props.show_project(self.session)
        self._status(f"NC mark  {settings['nc_mark']}")

    def set_xref_rung_pitch(self, pitch_mm: float) -> None:
        settings = self.session.set_cross_reference(rung_pitch_mm=float(pitch_mm))
        self.canvas.reload()
        self.props.show_project(self.session)
        self._status(f"NEMA line pitch  {settings['rung_pitch_mm']:g} mm")

    def choose_xref_rung_pitch(self) -> None:
        current = float(self.session.cross_reference_settings().get("rung_pitch_mm") or 12.7)
        value, ok = QInputDialog.getDouble(
            self,
            "NEMA line pitch",
            "Rung pitch (mm). 12.7 mm is 0.5 in — line = sheet×100 + rung.",
            current,
            2.5,
            50.0,
            2,
        )
        if ok:
            self.set_xref_rung_pitch(value)

    def edit_address(self, ref: str) -> None:
        try:
            comp = self.session.sheet().component(ref)
        except KeyError:
            return
        ip0 = str(comp.properties.get("ip") or comp.properties.get("address") or "")
        node0 = str(comp.properties.get("node") or comp.properties.get("station") or "")
        ip, ok = QInputDialog.getText(self, "Set address", f"{ref}  IP / address:", text=ip0)
        if not ok:
            return
        node, ok = QInputDialog.getText(self, "Set address", f"{ref}  node / station:", text=node0)
        if not ok:
            return
        self.session.set_address(ref, ip=ip, node=node)
        self.canvas.reload()
        self.props.show_component(self.session, ref)
        self.bom.refresh()
        self._status(f"{ref}  {ip.strip() or '—'}  node {node.strip() or '—'}")

    def edit_object(self, ref: str) -> None:
        from panelcad.gui.object_editor import ObjectEditor

        dlg = ObjectEditor(self.session, ref, self)
        if dlg.exec():
            self.canvas.reload()
            self.props.show_component(self.session, ref)
            self.bom.refresh()
            self._status(f"updated {ref} ports")

    def show_troubleshoot(self) -> None:
        """Open the guided fault-finding dock for the drawing that is open."""

        if getattr(self.troubleshoot, "_session", None) is not self.session:
            self.troubleshoot.set_session(self.session)
        self.troubleshoot_wrap.show()
        self.troubleshoot_wrap.raise_()
        self.resizeDocks([self.troubleshoot_wrap], [320], Qt.Orientation.Vertical)
        self._offer_hint("troubleshoot")

    def _troubleshoot_settings(self) -> None:
        self.open_settings()
        self.troubleshoot.sync_explain()

    def _highlight_troubleshoot(self, spec: dict) -> None:
        if not spec or spec.get("clear"):
            self.canvas.clear_trace_highlight()
            return
        sheet_id = str(spec.get("sheet") or "")
        if sheet_id and sheet_id != self.session.active_sheet_id:
            try:
                self.session.project.sheet(sheet_id)
            except KeyError:
                sheet_id = ""
            else:
                self._switch_sheet(sheet_id)
        self.canvas.highlight_trace(
            list(spec.get("devices") or []),
            list(spec.get("wires") or []),
            focus=str(spec.get("focus") or ""),
            focus_wire=str(spec.get("focus_wire") or ""),
            sheet_id=sheet_id,
        )
        focus = str(spec.get("focus") or "")
        wire = str(spec.get("focus_wire") or "")
        detail = " ".join(part for part in (focus, f"wire {wire}" if wire else "") if part)
        if detail:
            self._status(f"troubleshoot {detail}")

    def show_motor_sizing(self) -> None:
        self.sizing_panel.load_project(self.session)
        self.sizing_wrap.show()
        self.sizing_wrap.raise_()
        self.resizeDocks([self.sizing_wrap], [320], Qt.Orientation.Vertical)

    def cutout_drawings(self) -> None:
        from panelcad.gui.cutout_dialog import CutoutDialog

        self.canvas.sync_positions()
        dialog = CutoutDialog(self.session, self)
        dialog.exec()
        self._check_runner.schedule()
        self._status("cutout drawings closed")

    def size_enclosure(self) -> None:
        from PySide6.QtWidgets import QDialog

        from panelcad.gui.enclosure_dialog import SizeEnclosureDialog

        dlg = SizeEnclosureDialog(self.session, self)
        dlg.exec()
        if dlg.applied:
            self._refresh_from_session(fit=True)
            box = (dlg.recommendation.get("enclosure") or {}).get("name") or "enclosure"
            self._status(f"applied {box}")
        elif dlg.result() == QDialog.DialogCode.Accepted:
            self._refresh_from_session(fit=False)

    def convert_drawing_standard(self) -> None:
        from panelcad.gui.convert_dialog import ConvertDialog

        dialog = ConvertDialog(self.session, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.canvas.reload()
            self.props.show_project(self.session)
            self.bom.refresh()
            self.sheet_bar.refresh()
            self._status("converted drawing standard")

    def thermal_report(self) -> None:
        from panelcad.gui.thermal_dialog import ThermalDialog

        dlg = ThermalDialog(self.session, self)
        dlg.exec()
        self._refresh_from_session(fit=False)

    def _selected_ref(self) -> str | None:
        items = [i for i in self.canvas.scene.selectedItems() if isinstance(i, ComponentItem)]
        return items[0].component_id if items else None

    def browse_catalog_parts(self) -> None:
        ref = self._selected_ref()
        if ref:
            self.assign_catalog_part(ref)
            return
        from panelcad.gui.part_picker import pick_catalog_part

        pick_catalog_part(self, self.session)

    def assign_catalog_part(self, ref: str) -> None:
        from panelcad.gui.part_picker import pick_catalog_part

        category = ""
        library_id = ""
        try:
            comp = None
            for sheet in self.session.project.sheets:
                try:
                    comp = sheet.component(ref)
                    break
                except KeyError:
                    continue
            if comp is not None:
                library_id = comp.library_id
                category = self.session.catalog.get(comp.library_id).category
        except KeyError:
            category = ""
        part = pick_catalog_part(
            self,
            self.session,
            category=category,
            library_id=library_id,
        )
        if part is None:
            return
        try:
            result = self.session.assign_part(ref, part.catalog, manufacturer=part.manufacturer)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Catalog part", str(exc))
            return
        self.canvas.reload()
        self.canvas.select_ref(ref)
        self.props.show_component(self.session, ref)
        self.bom.refresh()
        self._check_runner.schedule()
        self._status(
            f"assigned {result['manufacturer']} {result['catalog']} → {ref} ({result['verified']})"
        )

    def confirm_part_envelope(self, ref: str) -> None:
        from panelcad.gui.envelope_dialog import confirm_envelope_dialog

        try:
            result = confirm_envelope_dialog(self, self.session, ref)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Confirm envelope", str(exc))
            return
        if result is None:
            return
        self.canvas.reload()
        self.canvas.select_ref(ref)
        self.props.show_component(self.session, ref)
        self.bom.refresh()
        self._check_runner.schedule()
        panel = result.get("panel") or {}
        self._status(
            f"confirmed envelope {ref} "
            f"{panel.get('width_mm')}×{panel.get('height_mm')}×{panel.get('depth_mm')} mm"
        )

    def pack_from_schematic(self) -> None:
        self.session.begin_edit("pack panel")
        self.canvas.sync_positions()
        try:
            try:
                if self.session.sheet().kind != "panel_layout":
                    panel = next((s for s in self.session.project.sheets if s.kind == "panel_layout"), None)
                    if panel is None:
                        self.session.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
                    else:
                        self.session.set_active_sheet(panel.id)
                result = self.session.layout_from_schematic()
            except ValueError as exc:
                QMessageBox.information(self, "Panel layout", str(exc))
                return
        finally:
            self.session.end_edit()
        if result.get("needs_info"):
            QMessageBox.information(
                self,
                "Panel layout",
                str(result.get("reason") or "Size the enclosure before building the layout."),
            )
            self._status(str(result.get("reason") or "layout needs an enclosure"))
            return
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.bom.refresh()
        unplaced = list(result.get("unplaced") or [])
        left_off = f", {len(unplaced)} not placed" if unplaced else ""
        if unplaced:
            QMessageBox.information(self, "Panel layout", _unplaced_layout_message(unplaced))
        self._status(
            f"packed {result['din']} DIN + {result['field']} field devices onto {result['rails']} rails"
            f"{left_off}"
        )

    def pack_io_ladder(self) -> None:
        self.canvas.sync_positions()
        existing = next(
            (s for s in self.session.project.sheets if s.kind == "io" or s.id == "io"),
            None,
        )
        if existing is not None:
            self.session.set_active_sheet(existing.id)
        elif self.session.sheet().kind != "io":
            self.session.add_sheet("io", title="I/O", kind="io")
        result = self.session.layout_io()
        self.canvas.reload()
        self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.bom.refresh()
        self._status(f"I/O ladder  {result['modules']} cards  {result['field']} field devices")

    def set_coil_style(self, style: str) -> None:
        self.session.set_title_block(coil_style=style)
        self.canvas.reload()
        self.props.show_project(self.session)
        self._status(f"coil style  {style}")

    def set_drawing_style(self, style: str) -> None:
        self.session.set_title_block(drawing_style=style)
        self.canvas.reload()
        self.props.show_project(self.session)
        self._status(f"drawing style  {self.session.project.title_block.drawing_style}")

    def set_drawing_standard(self, standard: str) -> None:
        self.session.set_title_block(standard=standard)
        self.canvas.reload()
        self.props.show_project(self.session)
        self._status(f"standard  {self.session.project.title_block.standard}")

    def _refresh_after_revision(self, status: str) -> None:
        self.canvas.reload()
        self.props.show_project(self.session)
        self.bom.refresh()
        self._status(status)

    def issue_revision(self) -> None:
        from panelcad.gui.revision_dialog import IssueRevisionDialog

        dialog = IssueRevisionDialog(self.session, self)
        if not dialog.exec():
            return
        info = dialog.result_info or {}
        self._refresh_after_revision(f"issued {info.get('rev', '')}")

    def show_revision_history(self) -> None:
        from panelcad.gui.revision_dialog import RevisionHistoryDialog

        RevisionHistoryDialog(self.session, self).exec()

    def compare_revisions(self) -> None:
        from panelcad.gui.revision_dialog import CompareRevisionsDialog

        CompareRevisionsDialog(self.session, self).exec()

    def clear_revision_clouds(self) -> None:
        result = self.session.clear_revision_clouds()
        self._refresh_after_revision(f"cleared {result.get('removed', 0)} clouds")

    def show_shortcuts(self) -> None:
        from panelcad.gui.shortcuts_dialog import ShortcutsDialog

        ShortcutsDialog(self).exec()

    def cleanup_sweep(self) -> None:
        self.canvas.sync_positions()
        result = self.session.cleanup()
        self.canvas.reload()
        n = result.get("count", 0)
        self._status(f"cleanup  {n} sheet(s)")

    def insert_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Company logo",
            "",
            "Images (*.png *.jpg *.jpeg *.svg *.bmp *.gif *.webp);;All files (*.*)",
        )
        if not path:
            return
        try:
            self.session.set_logo(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Logo failed", str(exc))
            return
        self.canvas.refresh_frame()
        self.props.show_project(self.session)
        note = "saved into project assets/" if self.session.path else "save the project to copy the logo into assets/"
        self._status(f"logo set — {note}")

    def clear_logo(self) -> None:
        self.session.clear_logo()
        self.canvas.refresh_frame()
        self.props.show_project(self.session)
        self._status("logo cleared")

    def _on_selection(self) -> None:
        items = [
            i
            for i in self.canvas.scene.selectedItems()
            if isinstance(i, ComponentItem) or getattr(i, "strip_block", False)
        ]
        if items:
            ref = items[0].component_id
            self.props.show_component(self.session, ref)
            self._select_power_supply(ref)
            self.sizing_panel.load_project(self.session)
        else:
            self.props.show_project(self.session)

    def _select_power_supply(self, ref: str) -> None:
        try:
            comp = self.session.sheet().component(ref)
            device = self.session.catalog.get(comp.library_id)
        except KeyError:
            return
        cat = device.category or ""
        if cat.startswith("power.supply") or cat.startswith("power.transformer"):
            self.power.select_supply(ref)
            self.power_wrap.show()
            self.power_wrap.raise_()

    def new_project(self) -> None:
        from panelcad.gui.style_dialog import choose_print_style

        choice = choose_print_style(self)
        if not choice:
            return
        self.session = Session.create("Untitled Panel")
        fields: dict[str, str] = {"drawing_style": choice}
        if choice == "ladder":
            fields["coil_style"] = "square"
        self.session.set_title_block(**fields)
        self._reattach()
        self._show_editor()
        self._status(f"new project  print style {choice}")

    def choose_print_style(self) -> None:
        from panelcad.gui.style_dialog import choose_print_style

        current = str(self.session.project.title_block.drawing_style or "")
        choice = choose_print_style(self, current=current)
        if not choice:
            return
        self.set_drawing_style(choice)

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open PanelCAD", "", "PanelCAD (*.pcad.yaml *.yaml)")
        if not path:
            return
        self.open_path(path)

    def save_project(self) -> bool:
        self.canvas.sync_positions()
        if self.session.path is None:
            return self.save_project_as()
        return self._commit_save(None)

    def save_project_as(self) -> bool:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save PanelCAD",
            save_as_start(self.session.path),
            "PanelCAD (*.pcad.yaml)",
        )
        if not path:
            return False
        if not path.endswith(".yaml"):
            path += ".pcad.yaml"
        return self._commit_save(path)

    def _commit_save(self, path: str | None) -> bool:
        from PySide6.QtWidgets import QApplication

        from panelcad.io.share import FileBusy, ProjectChanged, ReadOnlyProject

        # Save a copy calls back into this method. A paint event must not.
        if getattr(self, "_saving", False) and not getattr(self, "_allow_nested_save", False):
            return False
        nested = getattr(self, "_saving", False)
        self._saving = True
        if not nested:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._status("Saving…")
            self._allow_nested_save = False
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
            self._allow_nested_save = True
        try:
            try:
                saved = self.session.save() if path is None else self.session.save(path)
            except ReadOnlyProject as exc:
                return self._save_blocked(exc)
            except ProjectChanged as exc:
                return self._save_conflict(exc)
            except FileBusy:
                return self._save_busy()
            except OSError as exc:
                return self._save_rejected(exc)
            self._after_save(saved)
            return True
        finally:
            if not nested:
                self._saving = False
                self._allow_nested_save = False
                QApplication.restoreOverrideCursor()

    def _save_busy(self) -> bool:
        QMessageBox.warning(
            self,
            "File busy",
            "The file is busy (sync or another program). Nothing was overwritten. "
            "Try Save again or Save a copy.",
        )
        return False

    def _save_rejected(self, exc: OSError) -> bool:
        QMessageBox.warning(self, "Can't save there", cant_save_message(exc))
        return False

    def _after_save(self, path: Path) -> None:
        self._remember_saved_project()
        self._note_yaml_mtime()
        self._disk_conflict_noted = False
        self.setWindowTitle(self._title())
        self._show_editor()
        note = self._remember_recent(str(path))
        self._status(self._save_status(path, note))

    def _save_blocked(self, exc) -> bool:
        holder = getattr(exc, "holder", None)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Project locked")
        if holder is not None and not getattr(holder, "unreadable", False):
            box.setText(f"This project is locked by {holder.label()}.")
        else:
            box.setText("This project is read-only.")
        box.setInformativeText("Save a copy, or cancel. The open file was not overwritten.")
        copy_btn = box.addButton("Save a copy", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not copy_btn:
            return False
        return self._save_copy()

    def _save_conflict(self, exc) -> bool:
        from panelcad.gui.share_dialog import ask_conflict, ask_diff

        choice = ask_conflict(self, exc.path, list(getattr(exc, "copies", []) or []))
        while choice == "review":
            choice = ask_diff(self, self.session.preview_diff())
        if choice == "reload":
            return self.refresh_from_disk(discard=True)
        if choice == "copy":
            return self._save_copy()
        return False

    def _save_copy(self) -> bool:
        from panelcad.io.share import copy_suggestion

        current = self.session.path
        suggestion = copy_suggestion(current) if current is not None else ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save a copy",
            suggestion,
            "PanelCAD (*.pcad.yaml)",
        )
        if not path:
            return False
        if not path.endswith(".yaml"):
            path += ".pcad.yaml"
        return self._commit_save(path)

    def refresh_from_disk(self, _checked: bool = False, *, discard: bool = False) -> bool:
        """Reload the project file. Read-only viewers use this to pick up a save.

        ``discard`` skips the unsaved-changes question. The conflict dialog
        already asked.
        """

        path = self.session.path
        if path is None or not Path(path).is_file():
            self._status("Nothing on disk to refresh.")
            return False
        if self._project_dirty() and not discard:
            from panelcad.gui.share_dialog import confirm_discard

            if not confirm_discard(self):
                return False
        try:
            opened = Session.open(path)
        except Exception as exc:  # noqa: BLE001
            from panelcad.io.yaml_format import describe_open_error

            QMessageBox.critical(self, "Refresh failed", describe_open_error(exc, path))
            return False
        self.session = opened
        self._reattach()
        self._show_editor()
        self._remember_saved_project()
        self._disk_conflict_noted = False
        if opened.conflict_copies:
            from panelcad.gui.share_dialog import warn_conflict_copies

            warn_conflict_copies(self, opened.conflict_copies)
        notice = opened.share_notice
        text = f"refreshed {Path(path).name}"
        self._status(f"{notice} — {text}" if notice else text)
        return True

    def show_project_activity(self) -> None:
        from panelcad.gui.share_dialog import ProjectActivityDialog

        ProjectActivityDialog(self.session, self).exec()

    def _resolve_share_open(self, opened: Session) -> bool:
        if not opened.read_only:
            return True
        from panelcad.gui.share_dialog import ask_locked

        choice = ask_locked(self, opened.lock_holder)
        if choice == "cancel":
            return False
        if choice == "takeover":
            try:
                opened.take_over_lock()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Take over", str(exc))
                return False
        return True

    def _heartbeat_share_lock(self) -> None:
        session = getattr(self, "_session", None)
        if session is None:
            return
        try:
            session.heartbeat_share_lock()
        except Exception:  # noqa: BLE001
            return
        self.setWindowTitle(self._title())
        self._refresh_status_widgets()

    def export_svg(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export SVG", "", "SVG (*.svg)")
        if path:
            self.session.export_svg(path)
            self._status(f"exported {path}")

    def export_dxf(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export DXF", "", "DXF (*.dxf)")
        if not path:
            return
        if not path.lower().endswith(".dxf"):
            path += ".dxf"
        try:
            self.session.export_dxf(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "DXF failed", str(exc))
            return
        self._status(f"exported {path}")

    def print_sheet(self) -> None:
        self.canvas.sync_positions()
        from PySide6.QtPrintSupport import QPrintPreviewDialog, QPrinter

        from panelcad.render.page import paint_project_on_device

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        def _paint(device) -> None:
            paint_project_on_device(
                device,
                self.session.project,
                self.session.catalog,
                base_dir=self.session.path.parent if self.session.path else None,
            )

        n = len(self.session.project.sheets)
        preview = QPrintPreviewDialog(printer, self)
        preview.setWindowTitle(f"Print — {n} sheet{'s' if n != 1 else ''} with title blocks")
        preview.paintRequested.connect(_paint)
        preview.exec()
        self.canvas.refresh_frame()
        self.canvas.fit_page()
        self._status("print preview closed — title block is at the bottom of the sheet (View → Fit page)")

    def export_pdf(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export PDF", "", "PDF (*.pdf)")
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            try:
                self.session.export_pdf(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "PDF failed", str(exc))
                return
            self.canvas.refresh_frame()
            self.canvas.fit_page()
            n = len(self.session.project.sheets)
            self._status(f"exported {path} — {n} page print set")

    def print_package(self) -> None:
        self.canvas.sync_positions()
        from panelcad.gui.package_dialog import PrintPackageDialog

        suggested = "print_package.pdf"
        if self.session.path is not None:
            suggested = str(self.session.path.with_name(f"{self.session.path.stem}_package.pdf"))
        dialog = PrintPackageDialog(self, suggested=suggested)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        opts = dialog.values()
        path = str(opts["path"])
        try:
            result = self.session.print_package(
                path,
                paper=str(opts["paper"]),
                sections=list(opts["sections"]),  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Print package failed", str(exc))
            return
        self.canvas.refresh_frame()
        self.canvas.fit_page()
        self._status(f"print package {result['path']} — {result['pages']} pages")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._fitted_once:
            self._fitted_once = True
            # A saved dock layout belongs to the editor. Restoring it here
            # puts the tool docks back on top of the welcome page, and marks
            # the layout applied so opening a project never restores it.
            if not self._on_start_page() and not self._layout_applied:
                self._restore_layout()
                self._layout_applied = True
                # A saved layout is not the narrow arrangement. Apply it again.
                self._narrow_docks_on = False
            if not self._on_start_page():
                QTimer.singleShot(0, self.canvas, self.canvas.fit_drawing)
        self._apply_screen_docks()
        self._hide_duplicate_dock_tabs()
        self._schedule_dock_tab_hide()

    def show_quote(self) -> None:
        self.quote.refresh()
        self.quote_wrap.show()
        self.quote_wrap.raise_()
        if not self.quote_wrap.isFloating():
            self.resizeDocks([self.quote_wrap], [480], Qt.Orientation.Vertical)
        self._status("Quote — files only, nothing is sent")

    def export_bom(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export BOM", "", "CSV (*.csv)")
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            self.session.export_bom(path, grouped=False)
            self._status(f"exported BOM {path}")

    def export_bom_grouped(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export grouped BOM", "", "CSV (*.csv)")
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            self.session.export_bom(path, grouped=True)
            self._status(f"exported grouped BOM {path}")

    def import_bom(self, path: str | None = None) -> None:
        """Import a parts-list CSV.

        File > Import passes a path when the user chooses "Open as BOM import?".
        The File menu action also emits its checked flag, which is not a path.
        """

        if not isinstance(path, str) or not path.strip():
            path, _ = QFileDialog.getOpenFileName(self, "Import BOM", "", "CSV (*.csv);;All files (*.*)")
        if not path:
            return
        self.canvas.sync_positions()
        try:
            result = self.session.import_bom(path, place_missing=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "BOM import failed", str(exc))
            return
        self.canvas.reload()
        self.props.show_project(self.session)
        self.bom.refresh()
        self.sheet_bar.refresh()
        self._report_bom_import(result)

    def _save_status(self, path: object, note: str) -> str:
        from panelcad.needs_info import save_status_note

        text = f"saved {path}"
        if note:
            text = f"{text} — {note}"
        quiet = save_status_note(self.session.project)
        if quiet:
            text = f"{text} — {quiet}"
        return text

    def _needs_action(self, action: str, ref: str) -> None:
        from panelcad.gui.needs_info_dialog import run_needs_action

        message = run_needs_action(self, self.session, action, ref)
        self._refresh_after_needs_info()
        if message:
            self._status(message)

    def _refresh_after_needs_info(self) -> None:
        self.canvas.reload()
        self.bom.refresh()
        self.props.show_project(self.session)
        self.sheet_bar.refresh()
        self._check_runner.schedule()

    def _report_bom_import(self, result: dict) -> None:
        from panelcad.io.bom import format_bom_import_report
        from panelcad.reconstruct.placeholder import is_needs_info_component

        report = format_bom_import_report(result)
        created = set(result.get("created") or [])
        needs = [
            comp
            for sheet in self.session.project.sheets
            for comp in sheet.components
            if comp.id in created and is_needs_info_component(comp)
        ]
        if needs:
            from panelcad.gui.needs_info_dialog import NeedsInfoImportDialog

            dialog = NeedsInfoImportDialog(self.session, result, self)
            choice = ""
            ref = ""
            if dialog.exec() == dialog.DialogCode.Accepted:
                choice = dialog.choice
                ref = dialog.ref
            self.bom.refresh()
            if choice == "bom":
                self.bom_wrap.show()
                self.bom_wrap.raise_()
            elif choice == "problems":
                self._check_runner.schedule()
                self.problems_wrap.show()
                self.problems_wrap.raise_()
            elif choice in {"match", "manual", "lookup_all"}:
                self._needs_action(choice, ref)
            self._status(f"imported BOM — {report.splitlines()[0]}")
            return
        if result.get("errors") or result.get("skipped") or result.get("warnings"):
            QMessageBox.warning(self, "BOM import", report)
        self._status(f"imported BOM — {report.splitlines()[0]}")

    def export_plc_tags(self) -> None:
        from panelcad.gui.plc_tag_dialog import PlcTagExportDialog

        stored = self.session.project.properties.get("analog_start")
        initial: int | None
        try:
            initial = None if stored in (None, "") else int(stored)
        except (TypeError, ValueError):
            initial = None
        dialog = PlcTagExportDialog(self, analog_start=initial)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        kwargs: dict[str, object] = {
            "target": str(vals["target"]),
            "prefix": str(vals["prefix"]),
            "name_from": str(vals["name_from"]),
            "logix_style": str(vals["logix_style"]),
            "logix_slot": int(vals["logix_slot"]),
        }
        if "analog_start" in vals:
            chosen = vals["analog_start"]
            previous = self.session.project.properties.get("analog_start")
            if chosen != previous and not (chosen is None and "analog_start" not in self.session.project.properties):
                with self.session.command("analog start"):
                    if chosen is None:
                        self.session.project.properties.pop("analog_start", None)
                    else:
                        self.session.project.properties["analog_start"] = chosen
            kwargs["analog_start"] = None if chosen is None else int(chosen)
        try:
            result = self.session.export_plc_tags(str(vals["path"]), **kwargs)
        except ValueError as exc:
            QMessageBox.warning(self, "PLC tags", str(exc))
            return
        note = str(result.get("note") or "")
        self._status(f"PLC tags {result['path']}  {result['count']} points")
        if result.get("verified") is False:
            QMessageBox.information(self, "PLC tags", note)

    def export_wires(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export wire list", "", "CSV (*.csv)")
        if path:
            self.session.export_wire_list(path)
            self._status(f"exported {path}")

    def export_from_to(self) -> None:
        from panelcad.gui.wire_dialogs import FromToDialog

        dialog = FromToDialog(self, suggested="from_to.csv")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        try:
            path = self.session.export_from_to(vals["path"], sort=vals["sort"])
        except ValueError as exc:
            QMessageBox.warning(self, "Wire List (From-To)", str(exc))
            return
        rows = self.session.from_to_rows(sort=vals["sort"])
        self._status(f"from-to {path}  {len(rows)} rows  sort {vals['sort']}")

    def export_wire_markers(self) -> None:
        from panelcad.gui.wire_dialogs import WireMarkerDialog

        dialog = WireMarkerDialog(self, suggested="wire_markers.csv")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        try:
            result = self.session.export_wire_markers(
                vals["path"],
                target=vals["target"],
                style=vals["style"],
                sort=vals["sort"],
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Wire markers", str(exc))
            return
        self._status(
            f"markers {result['path']}  {result['rows']}  {result['target']}  "
            f"{result['style']}  {result['confidence']}"
        )
        if result.get("confidence") == "unverified":
            QMessageBox.information(self, "Wire markers", str(result.get("note") or ""))

    def edit_wire_spec(self, wire_id: str) -> None:
        from panelcad.gui.wire_dialogs import WireSpecDialog

        try:
            wire = self.session.find_wire(wire_id=wire_id)
        except (KeyError, ValueError):
            return
        dialog = WireSpecDialog(
            self,
            caption=f"{wire.frm}  →  {wire.to}",
            size=wire.gauge,
            color=wire.color,
            wire_type=wire.wire_type,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        self.session.set_wire_spec(
            wire_id,
            gauge=vals["size"],
            color=vals["color"],
            wire_type=vals["type"],
        )
        self.canvas._redraw_wires()
        self.bom.refresh()
        self._status(f"{wire.id}  {vals['size'] or '-'}  {vals['color'] or '-'}  {vals['type'] or '-'}")

    def edit_net_wire_defaults(self) -> None:
        from panelcad.gui.wire_dialogs import NetWireDialog

        nets: list[str] = []
        for sheet in self.session.project.sheets:
            for wire in sheet.wires:
                name = str(wire.net or "").strip()
                if name and name not in nets:
                    nets.append(name)
        dialog = NetWireDialog(self, nets=sorted(nets))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dialog.values()
        try:
            result = self.session.set_net_wire_spec(
                vals["net"],
                size=vals["size"],
                color=vals["color"],
                wire_type=vals["type"],
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Net wire defaults", str(exc))
            return
        self._status(
            f"net {result['net']}  size {result['size'] or '-'}  "
            f"color {result['color'] or '-'}  type {result['type'] or '-'}"
        )

    def _export_vendor_strip(self, vendor: str, file_format: str | None = None) -> None:
        strip = ""
        sheet = self.session.sheet()
        if sheet.kind == "terminal" and str(sheet.strip or "").strip():
            strip = str(sheet.strip)
        else:
            names = [row["strip"] for row in self.session.list_terminal_strips() if row["terminals"]]
            if len(names) == 1:
                strip = names[0]
        if not strip:
            QMessageBox.information(self, "Terminal strip", "Open a terminal strip first, or keep only one strip in the job.")
            return
        xml = str(file_format or "").lower() == "xml" or str(vendor).lower() in {"clipx", "wagoxml", "smartdesignerxml"}
        suffix = ".xml" if xml else ".csv"
        filt = "XML (*.xml)" if xml else "CSV (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, f"Export {strip}", f"{strip}{suffix}", filt)
        if not path:
            return
        if xml and not path.lower().endswith(".xml"):
            path += ".xml"
        if not xml and not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            result = self.session.export_terminal_strip(path, strip, vendor, file_format=file_format)
        except ValueError as exc:
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._status(f"{result['product']}  {result['format']}  {result['rows']} rows  {path}")
        warnings = result.get("warnings") or []
        if warnings:
            text = "\n".join(str(item.get("message") or item) for item in warnings[:12])
            extra = "" if len(warnings) <= 12 else f"\n… {len(warnings) - 12} more"
            QMessageBox.warning(self, "Unmapped articles", text + extra)

    def _export_report(self, title: str, writer) -> None:
        path, _ = QFileDialog.getSaveFileName(self, title, "", "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        writer(path)
        self._status(f"exported {path}")

    def edit_cables(self) -> None:
        from panelcad.gui.cable_dialog import CableEditorDialog

        dialog = CableEditorDialog(self.session, self)
        dialog.exec()
        self.canvas.reload()
        count = len(self.session.project.cables)
        self._status(f"{count} field cable(s)")

    def number_wires(self) -> None:
        result = self.session.number_wires()
        self.canvas.reload()
        self._status(
            f"wire numbers on {result['assigned']} wires ({result['nets']} nets, {result.get('scheme') or 'sheet_line'})"
        )

    def update_cross_references(self) -> None:
        rows = self.session.update_cross_references()
        self.canvas.reload()
        self._status(f"cross-reference on {len(rows)} symbols")

    def assign_item_numbers(self) -> None:
        count = self.session.assign_item_numbers()
        self.canvas.reload()
        self.bom.refresh()
        self._status(f"item numbers on {count} devices")

    def regenerate_io_sheets(self) -> None:
        try:
            result = self.session.io_generate_sheets()
        except ValueError as exc:
            QMessageBox.warning(self, "I/O sheets", str(exc))
            return
        self.canvas.reload()
        self._status(f"I/O sheets {', '.join(result.get('sheets') or [])}")

    def import_io(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import PLC I/O", "", "CSV (*.csv)")
        if not path:
            return
        result = self.session.import_io_list(path)
        self.canvas.reload()
        msg = f"updated {len(result['updated'])}"
        if result["errors"]:
            QMessageBox.warning(self, "PLC I/O import", msg + "\n\n" + "\n".join(result["errors"][:12]))
        self._status(f"PLC I/O — {msg}")

    def export_json(self) -> None:
        self.canvas.sync_positions()
        path, _ = QFileDialog.getSaveFileName(self, "Export JSON", "", "JSON (*.json)")
        if path:
            if not path.lower().endswith(".json"):
                path += ".json"
            self.session.export_json(path)
            self._status(f"exported {path}")

    def export_package(self) -> None:
        self.canvas.sync_positions()
        dest = QFileDialog.getExistingDirectory(self, "Export package folder")
        if not dest:
            return
        try:
            path = self.session.export_package(dest)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._status(f"exported package {path}")

    def _on_action_hover(self, action) -> None:
        topic = str(action.property("guidance_help") or "")
        reason = str(action.property("guidance_reason") or "")
        tip = reason or str(action.property("guidance_tip") or action.statusTip() or "")
        if tip:
            self.statusBar().showMessage(tip)
        self._status_help_topic = topic
        if hasattr(self, "_learn_more"):
            self._learn_more.setVisible(bool(topic))

    def _open_status_help(self) -> None:
        self.show_help(self._status_help_topic or "start")

    def show_help(self, topic: str = "start") -> None:
        self._ensure_help_dock()
        self.help_panel.show_topic(str(topic or "start"))
        self._show_help_dock()

    def show_checklists(self, *_args) -> None:
        self._ensure_help_dock()
        self.help_panel.show_tasks()
        self._show_help_dock()

    def _show_help_dock(self) -> None:
        narrow = self.width() < NARROW_WINDOW_PX
        self._place_help_dock(narrow=narrow)
        self.help_wrap.show()
        self.help_wrap.raise_()
        if self.help_wrap.isFloating():
            return
        if narrow:
            self.resizeDocks([self.prop_wrap], [280], Qt.Orientation.Horizontal)
        else:
            self.resizeDocks([self.help_wrap], [360], Qt.Orientation.Vertical)

    def _place_help_dock(self, *, narrow: bool) -> None:
        wrap = getattr(self, "help_wrap", None)
        if not isinstance(wrap, QDockWidget):
            return
        if narrow:
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, wrap)
            self.tabifyDockWidget(self.prop_wrap, wrap)
            return
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, wrap)
        self.tabifyDockWidget(self.bom_wrap, wrap)

    def _ensure_help_dock(self) -> None:
        if getattr(self, "help_wrap", None) is not None:
            return
        from panelcad.gui.guidance import annotate

        self.help_wrap = _ToolDock("Help", self)
        self.help_wrap.setObjectName("helpDock")
        self.help_wrap.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.help_wrap.setWidget(self.help_panel)
        self.help_panel.show()
        narrow = self.width() < NARROW_WINDOW_PX
        if narrow:
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.help_wrap)
            self.tabifyDockWidget(self.prop_wrap, self.help_wrap)
        else:
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.help_wrap)
            self.tabifyDockWidget(self.bom_wrap, self.help_wrap)
        annotate(self.help_wrap, "helpDock")

    def _install_learning(self) -> None:
        from panelcad.gui.guidance import HelpPanel, TourCard, WhatsThisController, annotate

        self.canvas.setObjectName("sheetCanvas")
        self.help_panel = HelpPanel(self)
        self.help_panel.hide()
        self.help_panel.action_requested.connect(self._run_task_action)
        self.help_wrap = None
        self.tour_card = TourCard(self)
        self.tour_card.advance.connect(self._tour_move)
        self.tour_card.pause.connect(self._tour_pause)
        self.tour_card.skip.connect(self._tour_skip)
        self.whats_this = WhatsThisController(self)
        self.whats_this.popup.learn_more.connect(self.show_help)
        for widget, key in (
            (self.lib_wrap, "libraryDock"),
            (self.prop_wrap, "inspectorDock"),
            (self.bom_wrap, "bomDock"),
            (self.problems_wrap, "problemsDock"),
            (self.quote_wrap, "quoteDock"),
            (self.troubleshoot_wrap, "troubleshootDock"),
            (self.next_steps_wrap, "nextStepsDock"),
            (self.ai_wrap, "aiDock"),
            (self.canvas, "sheetCanvas"),
            (self.toolbar, "mainToolbar"),
        ):
            annotate(widget, key)

    def offer_welcome(self) -> None:
        import os

        from panelcad.gui.splash import close_splash
        from panelcad.guidance import welcome_pending

        close_splash()
        if os.environ.get("PANELCAD_NO_WELCOME") == "1":
            return
        if not welcome_pending():
            return
        self.show_welcome()

    def show_welcome(self, *_args) -> None:
        from panelcad.gui.guidance import WelcomeDialog
        from panelcad.gui.splash import close_splash
        from panelcad.guidance import set_welcome_status

        close_splash()
        dialog = WelcomeDialog(self)
        dialog.exec()
        if dialog.choice == "tour":
            set_welcome_status("done")
            self.start_tour(True)
            return
        if dialog.choice == "help":
            set_welcome_status("done")
            self.show_help("start")
            return
        if dialog.choice == "start" or (not dialog.choice and dialog.dont_again.isChecked()):
            set_welcome_status("dismissed")

    def start_tour(self, restart: bool = True) -> None:
        from panelcad.guidance import set_tour_state, set_welcome_status, tour_state

        set_welcome_status("done")
        if self._on_start_page():
            self._show_editor()
        index = 0 if restart else int(tour_state()["index"])
        set_tour_state(status="active", index=index)
        self._show_tour_step(index)

    def resume_tour(self, *_args) -> None:
        from panelcad.guidance import set_tour_state, set_welcome_status, tour_state

        set_welcome_status("done")
        state = tour_state()
        index = 0 if state["status"] == "done" else int(state["index"])
        if self._on_start_page():
            self._show_editor()
        set_tour_state(status="active", index=index)
        self._show_tour_step(index)

    def _show_tour_step(self, index: int) -> None:
        from panelcad.learn import TOUR_STEPS

        step = TOUR_STEPS[index]
        target = self.findChild(QWidget, step["target"])
        if isinstance(target, QDockWidget):
            target.show()
            target.raise_()
            # Windows applies the tab switch on the next event pass. Placing
            # before that uses the dock's stale geometry and the card misses
            # the window.
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        self.tour_card.set_step(index)
        visible = target if target is not None and target.isVisible() else None
        self.tour_card.place_near(visible)
        self._status(f"Tour: {step['title']}")

    def _tour_move(self, delta: int) -> None:
        from panelcad.guidance import set_tour_state, tour_state
        from panelcad.learn import TOUR_STEPS

        index = int(tour_state()["index"]) + int(delta)
        if index < 0:
            index = 0
        if index >= len(TOUR_STEPS):
            set_tour_state(status="done", index=len(TOUR_STEPS) - 1)
            self.tour_card.hide()
            self._status("Tour finished. F1 opens the how-tos.")
            return
        set_tour_state(status="active", index=index)
        self._show_tour_step(index)

    def _tour_pause(self) -> None:
        from panelcad.guidance import set_tour_state, tour_state

        set_tour_state(status="paused", index=int(tour_state()["index"]))
        self.tour_card.hide()
        self._status("Tour hidden. Help → Resume tour continues here.")

    def _tour_skip(self) -> None:
        from panelcad.guidance import set_tour_state, tour_state

        set_tour_state(status="dismissed", index=int(tour_state()["index"]))
        self.tour_card.hide()
        self._status("Tour skipped. Help → Take the tour starts it again.")

    def _pause_tour_on_close(self) -> None:
        from panelcad.guidance import set_tour_state, tour_state

        if tour_state()["status"] == "active":
            set_tour_state(status="paused")
        controller = getattr(self, "whats_this", None)
        if controller is not None:
            controller.shutdown()

    def enter_whats_this(self, *_args) -> None:
        self.whats_this.enter()
        self._status("What's this: click a control. Esc cancels.")

    def _run_task_action(self, action: str) -> None:
        if action == "new-project":
            self.new_project()
        elif action == "save":
            self.save_project_as()
        elif action == "sample":
            self.open_sample_project()
        elif action == "library":
            self._add_from_catalog()
        elif action == "catalog":
            self.browse_catalog_parts()
        elif action == "wire":
            self._start_wire_mode()
        elif action == "select":
            self.canvas.set_select_mode()
        elif action == "bom":
            self.bom_wrap.show()
            self.bom_wrap.raise_()
            self._offer_hint("bom")
        elif action == "quote":
            self.show_quote()
        elif action == "problems":
            self.problems_wrap.show()
            self.problems_wrap.raise_()
        elif action == "print":
            self.print_package()
        elif action == "print-help":
            self.show_help("print")
        elif action == "csv":
            self.import_csv()
        elif action == "csv-help":
            self.show_help("csv")
        elif action == "wires-import":
            self.import_wires()
        elif action == "print-import":
            self.import_print()
        elif action == "print-import-help":
            self.show_help("print-import")
        elif action == "troubleshoot":
            self.troubleshoot_wrap.show()
            self.troubleshoot_wrap.raise_()
            self._offer_hint("troubleshoot")
        elif action == "troubleshoot-help":
            self.show_help("troubleshoot")

    def import_csv(self, *_args) -> None:
        """Open the parts-list guide, then import the file the person picks."""

        from panelcad.gui.guidance import CsvImportDialog

        dialog = CsvImportDialog(self)
        code = dialog.exec()
        if dialog.help_wanted:
            self.show_help("csv")
            return
        if code != QDialog.DialogCode.Accepted:
            return
        path = dialog.path_text()
        if path:
            self.import_bom(path)

    def _offer_hint(self, hint_id: str) -> bool:
        if not hasattr(self, "_hint_banner"):
            return False
        return self._hint_banner.offer(hint_id)

    def _start_wire_mode(self) -> None:
        self.canvas.set_wire_mode()
        self._offer_hint("wire")

    def _add_from_catalog(self) -> None:
        self.lib_wrap.show()
        self.lib_wrap.raise_()
        self._offer_hint("library")

    def _refresh_command_state(self) -> None:
        from panelcad.gui.guidance import enable_action, explain_disabled

        session = getattr(self, "session", None)
        watch = getattr(session, "watch_history", None)
        if callable(watch):
            # Every session this window shows, including one opened or
            # recovered later. watch_history ignores a repeat.
            watch(self._refresh_command_state)
        undo = getattr(self, "_undo_action", None)
        redo = getattr(self, "_redo_action", None)
        if undo is not None:
            if self.session.can_undo():
                enable_action(undo)
            else:
                explain_disabled(
                    undo,
                    "Undo is off: nothing to undo yet. Make a change, then Undo brings it back.",
                )
        if redo is not None:
            if self.session.can_redo():
                enable_action(redo)
            else:
                explain_disabled(
                    redo,
                    "Redo is off: there is nothing to put back. Undo something first.",
                )
        if hasattr(self, "_closed_menu"):
            self._refresh_closed_menu()

    def _on_drawing_changed(self) -> None:
        self._sync_empty_states()
        self._refresh_command_state()
        if self._switching_sheet:
            return
        self._schedule_next_steps()

    def _schedule_next_steps(self) -> None:
        if not hasattr(self, "next_steps"):
            return
        counts = self.problems.counts()
        self.next_steps.set_problem_count(counts["error"] + counts["warning"] + counts["info"])
        self.next_steps.schedule()

    def _sync_empty_states(self) -> None:
        if not hasattr(self, "_sheet_empty"):
            return
        from panelcad.guidance import empty_sheet_copy

        try:
            sheet = self.session.sheet()
        except Exception:  # noqa: BLE001
            self._sheet_empty.set_state("", ())
            return
        message, buttons = empty_sheet_copy(
            sheet.kind, components=len(sheet.components), wires=len(sheet.wires)
        )
        self._sheet_empty.set_state(message, buttons)

    def _empty_sheet_action(self, ident: str) -> None:
        if ident == "layout":
            self.pack_from_schematic()
            self._offer_hint("layout")
        elif ident == "catalog":
            self._add_from_catalog()
        elif ident == "import":
            self.import_csv()
            self._offer_hint("import")
        self._sync_empty_states()
        self._schedule_next_steps()

    def _apply_next_steps_pref(self) -> None:
        from panelcad.guidance import next_steps_open

        if next_steps_open():
            return
        self._suspend_guidance_persist = True
        try:
            self.next_steps_wrap.hide()
        finally:
            self._suspend_guidance_persist = False

    def _hide_next_steps(self) -> None:
        self._suspend_guidance_persist = True
        try:
            self.next_steps_wrap.hide()
        finally:
            self._suspend_guidance_persist = False

    def _do_next_step(self, step_id: str) -> None:
        if step_id == "enclosure":
            self.size_enclosure()
        elif step_id == "devices":
            self._add_from_catalog()
        elif step_id == "wires":
            self._start_wire_mode()
        elif step_id == "parts":
            self.bom_wrap.show()
            self.bom_wrap.raise_()
            self._offer_hint("bom")
        elif step_id == "layout":
            self.pack_from_schematic()
            self._offer_hint("layout")
        elif step_id == "print":
            self.print_package()
        self._schedule_next_steps()

    def open_sample_project(self) -> None:
        from panelcad.examples.sample_catalog import copy_sample_project
        from panelcad.settings import config_dir

        try:
            dest = copy_sample_project(config_dir() / "samples")
        except OSError as exc:
            QMessageBox.critical(self, "Open sample", str(exc))
            return
        self.open_path(str(dest))
        self._status(f"opened sample {dest.name}")

    def _offer_import_fix(self, path: str, exc: Exception) -> None:
        from panelcad.guidance import apply_import_fix, classify_import
        from panelcad.gui.guidance import ImportFixDialog

        advice = classify_import(path)
        from panelcad.diag.log import log_event

        log_event("import", str(exc))
        # A route we understand already says what the file is. The sniff error
        # would repeat the title line or the raw parser text under that.
        shown = "" if advice.route not in {"", "unknown"} else str(exc)
        dialog = ImportFixDialog(advice, shown, self)
        dialog.exec()
        if dialog.choice == "help":
            self.show_help(advice.help_topic)
            return
        if dialog.choice == "browse":
            if advice.route == "bom":
                self.import_bom()
            elif advice.route == "wires":
                self.import_wires()
            elif advice.route == "io":
                self.import_io()
            else:
                self.import_any()
            return
        if dialog.choice != "import":
            return
        if advice.route == "print":
            self.import_print()
            return
        try:
            result = apply_import_fix(self.session, path, advice)
        except Exception as failed:  # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(failed))
            return
        self._finish_import_result(result)

    def _finish_import_result(self, result: dict) -> None:
        self._reattach()
        if result.get("kind") == "bom":
            self._report_bom_import(result)
        else:
            self._status(f"imported {result.get('kind', 'file')}")
        self._schedule_next_steps()
        self._refresh_command_state()

    def open_vision_settings(self) -> None:
        from panelcad.gui.vision_settings import VisionSettingsDialog

        VisionSettingsDialog(self).exec()

    def import_print(self) -> None:
        from panelcad.gui.import_wizard import ImportWizard

        wizard = ImportWizard(self)
        if not wizard.exec():
            return
        self._import_job = wizard.job
        if wizard.session is not None:
            self.session = wizard.session
            self._reattach()
        if self._import_job is not None:
            self._show_import_review(self._import_job)
            self.import_review_wrap.show()
            self.import_review_wrap.raise_()
        self._status("print import")

    def _show_import_review(self, job) -> None:
        self.import_review.set_items(job.review_items())
        report = job.diagnostics_public() if hasattr(job, "diagnostics_public") else {}
        self.import_review.set_diagnostics(report)

    def _import_decision(self, item_id: str, action: str, text: str) -> None:
        job = getattr(self, "_import_job", None)
        if job is None:
            self._status("no print import is open")
            return
        try:
            job.decide(item_id, action, text or None)
        except Exception as exc:  # noqa: BLE001
            self._status(str(exc))
            return
        self._show_import_review(job)
        self._status(f"print review {action}")

    def _import_rerun(self, tile_id: str, page_id: str) -> None:
        job = getattr(self, "_import_job", None)
        if job is None:
            return
        try:
            job.rerun(tile_id=tile_id or None, page_id=page_id or None)
        except Exception as exc:  # noqa: BLE001
            self._status(str(exc))
            return
        self._show_import_review(job)
        self._status("re-ran print import")

    def import_any(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import",
            "",
            "All supported (*.pcad.yaml *.yaml *.yml *.json *.csv);;Packages / folders (*);;All files (*.*)",
        )
        folder = None
        if not path:
            folder = QFileDialog.getExistingDirectory(self, "Import package folder")
            if not folder:
                return
            path = folder
        self.canvas.sync_positions()
        try:
            result = self.session.import_any(path, replace=False)
        except Exception as exc:  # noqa: BLE001
            self._offer_import_fix(str(path), exc)
            return
        self._finish_import_result(result)

    def import_wires(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import wires", "", "CSV (*.csv)")
        if not path:
            return
        result = self.session.import_wires(path)
        self.canvas.reload()
        self.bom.refresh()
        self._status(f"wires added {len(result.get('added', []))}")

    def edit_library_device(self, library_id: str | None, *, duplicate: bool = False) -> None:
        from panelcad.gui.library_editor import LibraryEditor

        device = None
        if library_id:
            try:
                device = self.session.catalog.get(library_id)
            except KeyError:
                self._status(f"unknown library_id {library_id}")
                return
        dlg = LibraryEditor(self.session, device, duplicate=duplicate, parent=self)
        if dlg.exec():
            self.library_dock.catalog = self.session.catalog
            self.library_dock._rebuild()
            self._status("library saved")

    def import_library(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import library", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        ids = self.session.import_library(path)
        self.library_dock.catalog = self.session.catalog
        self.library_dock._rebuild()
        self._status(f"imported {len(ids)} library devices")

    def delete_selected(self) -> None:
        self.canvas._delete_selected()
        self.props.show_project(self.session)
        self.bom.refresh()

    def _delete_ref(self, ref: str) -> None:
        self.canvas.sync_positions()
        try:
            self.session.delete_component(ref)
        except KeyError:
            return
        self.canvas.reload()
        self.props.show_project(self.session)
        self.bom.refresh()
        self._status(f"deleted {ref}")

    def _oem_update(self, refs: list) -> None:
        self._queue_oem([str(r) for r in refs if r])

    def _oem_update_all(self) -> None:
        self._queue_oem(None)

    def _queue_oem(self, refs: list[str] | None) -> None:
        try:
            result = self.session.request_oem_update(refs)
        except ValueError as exc:
            QMessageBox.information(self, "OEM update", str(exc))
            return
        self.ai_wrap.show()
        self.ai_wrap.raise_()
        n = result.get("count") or 0
        self._status(f"OEM update queued for {n} part(s) — AI will pull size, pins, and a photo")
        self.bom.refresh()

    def _place_from_bom(self, library_id: str, ref: str) -> None:
        sheet = self.session.sheet()
        self.canvas.sync_positions()
        try:
            result = self.session.place_from_bom(library_id, ref=ref or None, x=40, y=40)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Place from BOM", str(exc))
            return
        self.canvas.reload()
        self.canvas.select_ref(result["id"])
        self.props.show_component(self.session, result["id"])
        self.bom.refresh()
        if result.get("placed"):
            self._status(f"BOM {result['id']} on {sheet.kind} ({result['library_id']})")
        else:
            self._status(f"{result['id']} already on this {sheet.kind} sheet")

    def _place_from_library(self, library_id: str) -> None:
        sheet = self.session.sheet()
        self.canvas.sync_positions()
        try:
            comp = self.session.place(library_id, x=40, y=40)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Place", str(exc))
            return
        self.canvas.reload()
        self.canvas.select_ref(comp.id)
        self.props.show_component(self.session, comp.id)
        self.bom.refresh()
        self._status(f"placed {comp.id} on {sheet.title}")

    def open_sccr(self) -> None:
        """UL 508A Supplement SB rating for the open project."""

        from panelcad.gui.sccr_dialog import SccrDialog

        SccrDialog(self.session, self).exec()
        self._refresh_from_session()

    def open_nameplate(self) -> None:
        """Reports → Nameplate for the open project."""

        from panelcad.gui.nameplate_dialog import NameplateDialog

        NameplateDialog(self.session, self).exec()
        self._refresh_from_session()

    def insert_circuit(self) -> None:
        """Insert a motor circuit on the open project. Does not replace it."""

        from PySide6.QtWidgets import QDialog

        from panelcad.gui.circuit_dialog import CircuitBuilderDialog

        standard = str(self.session.project.title_block.standard or "NEMA/ANSI")
        dlg = CircuitBuilderDialog(self, standard=standard)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self.session.insert_circuit(**dlg.values())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Circuit Builder", str(exc))
            return
        self._refresh_from_session(fit=True)
        self._show_editor()
        comps = result.get("components") or []
        if comps:
            self.canvas.select_ref(str(comps[0]["id"]))
        kind = str(result.get("kind") or "dol").replace("_", " ")
        layout = result.get("layout") or {}
        panel = layout.get("sheet")
        extra = f", layout {panel}" if panel else ""
        if result.get("control_sheet"):
            extra += f", control {result['control_sheet']}"
        warns = [str(item) for item in (result.get("warnings") or []) if str(item).strip()]
        if not warns:
            warns = [str(item) for item in (layout.get("warnings") or []) if str(item).strip()]
        status_warns = circuit_warning_status_text(warns)
        if status_warns:
            extra += "  warning: " + status_warns
        self._status(
            f"inserted {kind} {result.get('standard') or standard} circuit "
            f"on {result.get('sheet')} ({len(comps)} devices{extra})"
        )
        dialog_text = circuit_warning_dialog_text(warns)
        if dialog_text:
            QMessageBox.warning(self, "Circuit Builder", dialog_text)

    def rename_device(self, ref: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "Rename", "Tag", text=ref)
        if not ok:
            return
        new = text.strip()
        if not new or new == ref:
            return
        try:
            self.session.rename_component(ref, new)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Rename", str(exc))
            return
        self._refresh_from_session()
        self._status(f"renamed {ref} to {new}")

    def insert_io_sheets(self) -> None:
        """Pick a PLC family and modules, then draw the I/O schematic sheets."""

        from PySide6.QtWidgets import QDialog

        from panelcad.gui.io_wizard import IoWizard

        dlg = IoWizard(self.session, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if dlg._applied:
                self.session.undo()
                self._refresh_from_session(fit=True)
            return
        self._refresh_from_session(fit=True)
        sheets = (dlg._result or {}).get("sheets") or []
        self._status(f"I/O sheets {', '.join(sheets)}" if sheets else "I/O sheets")

    def open_terminal_strip(self) -> None:
        from PySide6.QtWidgets import QDialog

        from panelcad.gui.terminal_dialog import TerminalStripDialog

        names = [row["strip"] for row in self.session.list_terminal_strips()]
        dlg = TerminalStripDialog(names, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.strip_name()
        if not name:
            QMessageBox.warning(self, "Terminal strip", "Type a strip name, such as X1.")
            return
        try:
            result = self.session.open_terminal_strip(name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._refresh_from_session(fit=True)
        n = sum(1 for item in result.get("items") or [] if item.get("kind") == "terminal")
        self._status(f"terminal strip {name}  {n} terminals")

    def _require_strip(self) -> str | None:
        sheet = self.session.sheet()
        if sheet.kind != "terminal" or not str(sheet.strip or "").strip():
            QMessageBox.information(
                self,
                "Terminal strip",
                "Open a terminal strip first (Insert → Terminal strip…).",
            )
            return None
        return str(sheet.strip)

    def _strip_selection(self, kind: str | None = None) -> list[str]:
        refs: list[str] = []
        for item in self.canvas.scene.selectedItems():
            if not getattr(item, "strip_block", False):
                continue
            if kind and getattr(item, "strip_kind", "") != kind:
                continue
            ref = getattr(item, "component_id", None)
            if ref:
                refs.append(str(ref))
        return refs

    def add_strip_terminal(self, library_id: str) -> None:
        strip = self._require_strip()
        if not strip:
            return
        selected = self._strip_selection("terminal")
        after = selected[0] if len(selected) == 1 else None
        try:
            result = self.session.add_terminal(strip, library_id=library_id, after=after)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._refresh_from_session()
        self.canvas.select_ref(result["ref"])
        self.props.show_component(self.session, result["ref"])
        self._status(f"added {result['ref']} on {strip} (level {result['terminal']})")

    def add_strip_partition(self) -> None:
        strip = self._require_strip()
        if not strip:
            return
        selected = self._strip_selection("terminal")
        after = selected[0] if selected else None
        try:
            result = self.session.add_strip_accessory(strip, "partition", after=after)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._refresh_from_session()
        self.canvas.select_ref(result["ref"])
        self._status(f"partition {result['ref']} after {result.get('after') or 'the last terminal'}")

    def jumper_selected_terminals(self) -> None:
        if not self._require_strip():
            return
        refs = self._strip_selection("terminal")
        if len(refs) < 2:
            QMessageBox.information(self, "Jumper", "Select at least two terminal blocks.")
            return
        try:
            result = self.session.jumper_terminals(refs)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Jumper", str(exc))
            return
        self._refresh_from_session()
        part = ""
        parts = result.get("parts") or []
        if parts:
            first = parts[0]
            part = f"  {first.get('part_number') or first.get('catalog') or ''}".rstrip()
        self._status(f"jumper {result['jumper']} on {', '.join(result['refs'])}{part}")

    def clear_selected_jumpers(self) -> None:
        if not self._require_strip():
            return
        refs = self._strip_selection("terminal")
        if not refs:
            QMessageBox.information(self, "Jumper", "Select the terminals that should lose the jumper.")
            return
        try:
            result = self.session.clear_terminal_jumpers(refs=refs)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Jumper", str(exc))
            return
        self._refresh_from_session()
        self._status(f"cleared jumper on {', '.join(result.get('cleared') or refs)}")

    def move_selected_terminal(self, direction: str) -> None:
        strip = self._require_strip()
        if not strip:
            return
        refs = self._strip_selection("terminal")
        if len(refs) != 1:
            QMessageBox.information(self, "Terminal strip", "Select one terminal block.")
            return
        try:
            self.session.move_terminal(strip, refs[0], direction)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._refresh_from_session()
        self.canvas.select_ref(refs[0])
        word = "earlier" if direction == "earlier" else "later"
        self._status(f"moved {refs[0]} {word}")

    def retag_open_strip(self) -> None:
        strip = self._require_strip()
        if not strip:
            return
        try:
            result = self.session.retag_terminal_strip(strip)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Terminal strip", str(exc))
            return
        self._refresh_from_session()
        renamed = result.get("renamed") or []
        self._status(f"renumbered {strip}  {len(renamed)} tag(s)")

    def load_example(self) -> None:
        session = _choose_template(self)
        if session is None:
            return
        self.session = session
        self._reattach()
        self._show_editor()

    def report_a_bug(self) -> None:
        from panelcad.gui.bug_report_dialog import BugReportDialog

        BugReportDialog(self, self.session).exec()

    def open_recovery(self, path: Path, project_path: Path | str | None = None) -> None:
        self.session = Session.open_recovery(path, project_path)
        self._reattach()
        if self.session.path is None:
            self._status(f"recovered {path.name} — unsaved")
        else:
            self._status(f"recovered {self.session.path.name}")

    def about(self) -> None:
        QMessageBox.information(self, "PanelCAD", about_text(__version__))

    def _startup_updates(self) -> None:
        from panelcad.gui.splash import close_splash
        from panelcad.update.manifest import UpdateError
        from panelcad.update.service import check_for_update
        from panelcad.update.settings import consume_notice, startup_check_due

        # A closed window must not touch the splash. Parentless single-shot
        # timers from an earlier MainWindow were closing a splash opened later.
        if not _qt_alive(self) or self._closing:
            return
        try:
            visible = self.isVisible()
        except RuntimeError:
            return
        if not visible:
            return
        if self._startup_updates_ran:
            return

        # The off-screen first paint and the recovery/welcome timer both call
        # processEvents. A dialog there sits under the splash, or inside the
        # recovery loop. Wait one beat instead of nesting it. The wait is
        # bounded so a defer flag that is never cleared cannot poll forever.
        offscreen = self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        waiting = offscreen or self._defer_startup_updates
        if waiting:
            self._startup_update_waits += 1
            if self._startup_update_waits > _STARTUP_UPDATE_POLLS:
                return
            QTimer.singleShot(50, self, self._startup_updates)
            return
        self._startup_updates_ran = True
        close_splash()
        try:
            self._run_startup_updates(
                consume_notice, startup_check_due, check_for_update, UpdateError
            )
        except BaseException:
            close_splash()
            raise

    def _run_startup_updates(
        self, consume_notice, startup_check_due, check_for_update, update_error
    ) -> None:
        try:
            notice = consume_notice()
        except update_error as exc:
            self._status(str(exc))
            notice = None
        if notice:
            self._show_update_notice(
                notice["version"],
                kind=notice.get("kind") or "update",
                message=notice.get("message") or "",
                log=notice.get("log") or "",
            )
        try:
            due = startup_check_due()
        except update_error as exc:
            self._status(str(exc))
            return
        if not due:
            return

        def work() -> None:
            try:
                report = check_for_update()
            except Exception as exc:  # noqa: BLE001 — startup check must not take down the window
                report = {"enabled": True, "error": str(exc)}
            self._startup_update_bridge.finished.emit(report)

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _on_startup_update_report(self, report: object) -> None:
        if isinstance(report, dict):
            self._present_update_report(report, automatic=True)

    def _show_update_notice(
        self,
        version: str,
        *,
        kind: str,
        message: str = "",
        log: str = "",
    ) -> None:
        from panelcad.gui.splash import close_splash
        from panelcad.gui.update_dialog import UpdateNoticeDialog

        close_splash()
        dialog = UpdateNoticeDialog(
            version, kind=kind, message=message, log=log, parent=self
        )
        dialog.exec()

    def _project_snapshot(self) -> str:
        from panelcad.io.yaml_format import dump_project

        self.canvas.sync_positions()
        return dump_project(self.session.project)

    def _remember_saved_project(self) -> None:
        self._saved_snapshot = self._project_snapshot()

    def _project_dirty(self) -> bool:
        return self._project_snapshot() != self._saved_snapshot

    def _ask_save_changes(self) -> str:
        """``save``, ``discard``, or ``cancel``. A clean drawing does not ask."""

        if not self._project_dirty():
            return "discard"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("PanelCAD")
        box.setText("This drawing has unsaved changes.")
        box.setInformativeText("Save them before installing or rolling back?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        return "cancel"

    def _confirm_unsaved(self) -> bool:
        """Run the normal unsaved-changes prompt. Cancel aborts the install or rollback."""

        choice = self._ask_save_changes()
        if choice == "cancel":
            return False
        if choice == "save":
            if not self.save_project():
                return False
            if self._project_dirty():
                return False
        return True

    def _run_off_ui(self, title: str, detail: str, work) -> tuple[bool, object]:
        """Run ``work`` off the UI thread. The dialog stays responsive and has Cancel."""

        from panelcad.gui.update_dialog import UpdateProgressDialog

        cancel_event = threading.Event()
        dialog = UpdateProgressDialog(title, detail, self, on_cancel=cancel_event.set)
        outcome: dict[str, object] = {}

        def target() -> None:
            try:
                outcome["result"] = work(cancel_event.is_set)
            except Exception as exc:  # noqa: BLE001 — shown in the dialog's caller
                outcome["error"] = exc

        thread = threading.Thread(target=target, name="panelcad-update", daemon=True)
        thread.start()
        timer = QTimer(self)

        def poll() -> None:
            if thread.is_alive():
                return
            timer.stop()
            if not dialog.cancelled:
                dialog.accept()

        timer.timeout.connect(poll)
        timer.start(50)
        dialog.exec()
        timer.stop()
        if dialog.cancelled:
            cancel_event.set()
            return True, None
        thread.join(timeout=2)
        if "error" in outcome:
            return False, outcome["error"]
        return False, outcome.get("result")

    def show_update_settings(self) -> None:
        from panelcad.gui.update_dialog import UpdateSettingsDialog, settings_from_dialog
        from panelcad.update.settings import load_settings, save_settings

        previous = load_settings()
        dialog = UpdateSettingsDialog(previous, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        save_settings(settings_from_dialog(dialog, previous))
        if dialog.check_now:
            self.check_for_updates()

    def check_for_updates(self) -> None:
        from panelcad.update.manifest import UpdateError
        from panelcad.update.service import check_for_update
        from panelcad.update.settings import config_dir

        def work(_cancel) -> dict:
            return check_for_update(config_dir=config_dir())

        cancelled, outcome = self._run_off_ui(
            "Checking for updates",
            "Reading the release feed.",
            work,
        )
        if cancelled:
            self._status("Update check cancelled.")
            return
        if isinstance(outcome, UpdateError):
            QMessageBox.warning(self, "PanelCAD updates", str(outcome))
            return
        if isinstance(outcome, Exception):
            QMessageBox.warning(self, "PanelCAD updates", str(outcome))
            return
        if not isinstance(outcome, dict):
            return
        self._present_update_report(outcome, automatic=False)

    def _present_update_report(self, report: dict, *, automatic: bool) -> None:
        from panelcad.gui.splash import close_splash
        from panelcad.gui.update_dialog import UpdateAvailableDialog
        from panelcad.update.service import startup_should_prompt
        from panelcad.update.settings import set_skip_version

        close_splash()
        if report.get("error"):
            if not automatic:
                QMessageBox.warning(self, "PanelCAD updates", str(report["error"]))
            else:
                self._status(str(report["error"]))
            return
        if not report.get("enabled"):
            if not automatic:
                QMessageBox.information(
                    self,
                    "PanelCAD updates",
                    str(
                        report.get("reason")
                        or "Updates are off until a feed is set in Help > Updates."
                    ),
                )
            return
        if automatic and not startup_should_prompt(report):
            return
        if not report.get("update_available"):
            if not automatic:
                QMessageBox.information(
                    self,
                    "PanelCAD updates",
                    str(report.get("reason") or f"PanelCAD {__version__} is up to date."),
                )
            return
        dialog = UpdateAvailableDialog(report, self)
        dialog.exec()
        if dialog.choice == "skip":
            set_skip_version(str(report.get("version") or ""))
            self._status(f"skipped {report.get('version')}")
        elif dialog.choice == "install":
            self.install_update_now(report)

    def install_update_now(self, report: dict | None = None) -> bool:
        """Install the manifest the dialog showed. Cancel on the save prompt aborts."""

        from PySide6.QtWidgets import QApplication

        from panelcad.update.service import install_update
        from panelcad.update.settings import config_dir

        if not self._confirm_unsaved():
            return False
        version = str((report or {}).get("version") or "")
        detail = f"Installing PanelCAD {version}." if version else "Installing the update."
        project = str(self.session.path) if self.session.path else None
        expected_version = str(report.get("version") or "") if report else None
        expected_sha = str(report.get("sha256") or "") if report else None

        def work(cancel) -> dict:
            return install_update(
                confirm=True,
                relaunch_project=project,
                config_dir=config_dir(),
                expected_version=expected_version or None,
                expected_sha256=expected_sha or None,
                cancel=cancel,
            )

        cancelled, outcome = self._run_off_ui("Installing update", detail, work)
        if cancelled:
            self._status("Update cancelled.")
            return False
        if isinstance(outcome, Exception):
            QMessageBox.warning(self, "PanelCAD updates", str(outcome))
            return False
        result = outcome if isinstance(outcome, dict) else {}
        if result.get("close_app"):
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return True
        return bool(result)

    def roll_back_update(self) -> bool:
        """Ask, then restore program files. Cancel on either prompt aborts."""

        from PySide6.QtWidgets import QApplication

        from panelcad.gui.update_dialog import RollbackDialog
        from panelcad.update.manifest import UpdateError
        from panelcad.update.service import rollback_update
        from panelcad.update.settings import config_dir, read_rollback

        try:
            info = read_rollback(config_dir())
        except UpdateError as exc:
            QMessageBox.warning(self, "Roll back", str(exc))
            return False
        if not info:
            QMessageBox.information(self, "Roll back", "No previous install is saved.")
            return False
        dialog = RollbackDialog(
            str(info.get("previous_version") or ""),
            str(info.get("backup") or ""),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.confirmed:
            return False
        if not self._confirm_unsaved():
            return False
        project = str(self.session.path) if self.session.path else None

        def work(_cancel) -> dict:
            return rollback_update(
                confirm=True,
                relaunch_project=project,
                config_dir=config_dir(),
            )

        cancelled, outcome = self._run_off_ui(
            "Rolling back",
            "Restoring program files from the saved copy.",
            work,
        )
        if cancelled:
            self._status("Roll back cancelled.")
            return False
        if isinstance(outcome, Exception):
            QMessageBox.warning(self, "Roll back", str(outcome))
            return False
        result = outcome if isinstance(outcome, dict) else {}
        if result.get("close_app"):
            app = QApplication.instance()
            if app is not None:
                app.quit()
        return True

    def _reattach(self) -> None:
        self.canvas.session = self.session
        self._check_runner.session = self.session
        self.library_dock.catalog = self.session.catalog
        self.library_dock._rebuild()
        self.canvas.reload()
        self.canvas.fit_page()
        self.props.show_project(self.session)
        self.bom.set_session(self.session)
        self.quote.set_session(self.session)
        self.power.set_session(self.session)
        self.sheet_bar.set_session(self.session)
        self.next_steps.set_session(self.session)
        if hasattr(self, "troubleshoot"):
            self.troubleshoot.set_session(self.session)
        self.setWindowTitle(self._title())
        self._bind_ai()
        self._sync_ai_page()
        self._check_runner.schedule()
        self._sync_empty_states()
        self._refresh_command_state()

    def _bind_ai(self) -> None:
        from panelcad.api.activity import journal_path, read_jsonl

        self._agent.session = self.session
        self.session._listeners = [self._on_ai_event]
        self._note_yaml_mtime()
        events = read_jsonl(journal_path(self.session.path))
        if events:
            self.ai_dock.load_events(events)
        self._sync_ai_page()

    def _submit_ai_prompt(self, text: str) -> None:
        raw = text.strip()
        if not raw:
            return
        self.ai_wrap.show()
        self.ai_wrap.raise_()
        if raw.startswith("{") and raw.endswith("}"):
            import json as _json

            from panelcad.api.activity import send

            try:
                msg = _json.loads(raw)
            except _json.JSONDecodeError:
                msg = None
            if isinstance(msg, dict) and msg.get("op"):
                if self.session.path is None:
                    QMessageBox.information(self, "AI prompt", "Save the project so the AI inbox has a path.")
                    return
                send(self.session.path, msg)
                self.session.emit("prompt", f"you → {msg.get('op')}", extra={"prompt": raw})
                self._status(f"queued op {msg.get('op')} for the AI")
                return
        try:
            result = self.session.run_prompt(raw)
        except Exception as exc:  # noqa: BLE001
            self.session.emit("say", str(exc), status="error", error=str(exc))
            self._status(f"prompt failed: {exc}")
            return
        self._refresh_from_session(fit=False)
        if result.get("ok"):
            did = "; ".join(result.get("did") or [])
            self._status(did or "prompt done")
        else:
            self._status(str(result.get("error") or "couldn't do that"))

    def _sync_ai_page(self) -> None:
        sheet = self.session.sheet()
        self.ai_dock.set_page(
            self.session.sheet_index() + 1,
            len(self.session.project.sheets),
            sheet.title,
            sheet.kind,
            sheet.id,
            self.session.project.name,
        )

    def _on_ai_event(self, event: dict) -> None:
        actor = event.get("actor") or {}
        if actor.get("kind") != "ai" and event.get("op") not in ("hello", "say", "idle", "prompt"):
            return
        self.ai_dock.apply_event(event)
        if str(event.get("status") or "") == "working":
            self._ai_idle_ticks = 0
        self._sync_ai_page()

    def _note_yaml_mtime(self) -> None:
        path = self.session.path
        if path is not None and path.is_file():
            self._yaml_mtime = path.stat().st_mtime
        else:
            self._yaml_mtime = 0.0

    def _refresh_from_session(self, *, fit: bool = False) -> None:
        self.canvas.session = self.session
        self.canvas.reload()
        if fit:
            self.canvas.fit_page()
        self.sheet_bar.refresh()
        self.props.show_project(self.session)
        self.bom.set_session(self.session)
        self.sizing_panel.load_project(self.session)
        self.quote.set_session(self.session)
        self.setWindowTitle(self._title())
        self._sync_ai_page()
        self._note_yaml_mtime()
        self._check_runner.session = self.session
        self._check_runner.schedule()
        self._sync_empty_states()
        self._refresh_command_state()
        self._schedule_next_steps()

    def _gui_update_install(self, msg: dict) -> dict:
        """Open the update dialog. ``confirm`` on an inbox line is not a click."""

        from panelcad.gui.update_dialog import UpdateAvailableDialog
        from panelcad.update.service import check_for_update
        from panelcad.update.settings import config_dir

        feed = msg.get("feed")

        def work(_cancel) -> dict:
            return check_for_update(
                feed=str(feed) if feed else None,
                config_dir=config_dir(),
            )

        cancelled, outcome = self._run_off_ui(
            "Checking for updates",
            "Reading the release feed.",
            work,
        )
        if cancelled:
            return {"id": msg.get("id"), "ok": False, "error": "waiting for the operator"}
        if isinstance(outcome, Exception):
            return {"id": msg.get("id"), "ok": False, "error": str(outcome)}
        report = outcome if isinstance(outcome, dict) else {}
        if not report.get("installable"):
            reason = str(report.get("reason") or "No update is available.")
            return {"id": msg.get("id"), "ok": False, "error": reason}
        dialog = UpdateAvailableDialog(report, self)
        dialog.exec()
        if dialog.choice != "install":
            return {"id": msg.get("id"), "ok": False, "error": "waiting for the operator"}
        if not self.install_update_now(report):
            return {"id": msg.get("id"), "ok": False, "error": "waiting for the operator"}
        return {"id": msg.get("id"), "ok": True, "result": {"choice": "install"}}

    def _inbox_handle(self, msg: dict) -> dict:
        """Run Session ops from the inbox. ask_style pops the same picker File → New uses."""

        op_name = str(msg.get("op") or "")
        if op_name == "update_install":
            return self._gui_update_install(msg)
        if op_name == "update_rollback":
            if self.roll_back_update():
                return {"id": msg.get("id"), "ok": True, "result": {"choice": "rollback"}}
            return {
                "id": msg.get("id"),
                "ok": False,
                "error": "waiting for the operator",
            }
        if op_name != "ask_style":
            return self._agent.handle(msg)
        from panelcad.gui.style_dialog import choose_print_style

        current = str(self.session.project.title_block.drawing_style or "")
        if current in {"ladder", "schematic", "freeform"}:
            return {"id": msg.get("id"), "ok": True, "result": self.session.ask_print_style()}
        self.session.emit(
            "say",
            "Which print style for schematic sheets — schematic, ladder, or freeform?",
            status="working",
        )
        choice = choose_print_style(self, current=current)
        if not choice:
            return {
                "id": msg.get("id"),
                "ok": False,
                "error": "operator cancelled print-style choice",
            }
        fields: dict[str, str] = {"drawing_style": choice}
        if choice == "ladder":
            fields["coil_style"] = "square"
        self.session.set_title_block(**fields)
        result = self.session.ask_print_style()
        self.session.emit("say", f"print style {choice}", status="idle")
        return {"id": msg.get("id"), "ok": True, "result": result}

    def _poll_ai(self) -> None:
        from panelcad.api.activity import DEFAULT_MODEL, drain_inbox, inbox_pending, peek_inbox

        path = self.session.path
        if path is None:
            return

        pending_ops = [str(msg.get("op") or "") for msg in peek_inbox(path)]
        ui_only = bool(pending_ops) and all(op.startswith("ui_") for op in pending_ops)
        if self.session.actor.get("kind") != "ai" and inbox_pending(path) and not ui_only:
            self.session.announce(DEFAULT_MODEL)
        replies = drain_inbox(path, self._inbox_handle)
        if replies:
            if ui_only:
                self._ai_idle_ticks = 0
                self._status("appearance updated")
                return
            if self._agent.session is not None and self._agent.session is not self.session:
                self.session = self._agent.session
                self._bind_ai()
            # The open canvas is still the pre-op scene. Reload it from the
            # model before save or the dirty snapshot, both of which read
            # scene positions. A same-tag device on another page is a
            # different placement and must not inherit this page's scene.
            self._refresh_from_session(fit=False)
            if any(r.get("ok") for r in replies) and self.session.path is not None:
                try:
                    self.session.save()
                    self._remember_saved_project()
                except Exception:  # noqa: BLE001
                    pass
                self._note_yaml_mtime()
            self._ai_idle_ticks = 0
            n_ok = sum(1 for r in replies if r.get("ok"))
            model = self.session.actor.get("model") or DEFAULT_MODEL
            self._status(f"AI  {model}  {n_ok} op(s) on {self.session.sheet().title}")
        else:
            self._ai_idle_ticks += 1
            if self.session.actor.get("kind") == "ai" and self.ai_dock.status_name == "working" and self._ai_idle_ticks > 25:
                self.session.set_idle()
        self._poll_yaml()

    def _poll_yaml(self) -> None:
        path = self.session.path
        if path is None or not path.is_file():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._yaml_mtime and mtime > self._yaml_mtime + 0.05:
            try:
                matches = self.session.disk_matches()
            except OSError:
                # A sync client still holds the file. Leave the remembered
                # mtime so a later tick can see the change, and do not tell
                # the user. The save path is what waits and retries.
                return
            if matches:
                self._note_yaml_mtime()
                return
            if self._project_dirty():
                if not self._disk_conflict_noted:
                    self._disk_conflict_noted = True
                    self._status(
                        "The project file changed on disk. Save will ask before replacing it."
                    )
                return
            try:
                opened = Session.open(path)
            except Exception:  # noqa: BLE001
                return
            self.session = opened
            self._reattach()
            self._remember_saved_project()
            self._disk_conflict_noted = False
            self._status(f"reloaded {path.name}  (AI / disk)")

    @property
    def session(self) -> Session:
        return self._session

    @session.setter
    def session(self, value: Session) -> None:
        old = getattr(self, "_session", None)
        self._session = value
        if old is not None and old is not value:
            try:
                old.release_share_lock()
            except Exception:  # noqa: BLE001
                pass
        try:
            from panelcad.diag.crash import set_recovery_session

            set_recovery_session(value)
        except Exception:  # noqa: BLE001
            return
