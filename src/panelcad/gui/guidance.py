"""Guidance widgets: why-disabled, import fix-it, empty states, help, hints, next steps.

Also the first-run welcome, the guided tour, the searchable Help panel,
task checklists, what's-this, and the CSV import guide.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.guidance import (
    HELP_TOPIC_ORDER,
    HINTS,
    NEXT_STEPS_DEBOUNCE_MS,
    STEP_ORDER,
    ChecklistStep,
    ImportAdvice,
    help_topic,
)

_FIXES: dict[int, Callable[[], None]] = {}


def explain_disabled(action: QAction, reason: str, fix_callable: Callable[[], None] | None = None) -> None:
    """Grey out ``action`` and say why, plus how to fix it.

    The reason is the tooltip and the status-bar tip. ``fix_callable`` is kept
    for a test or a button that runs the fix. Disabled menu items still report
    the status tip when they are highlighted.
    """

    text = str(reason or "").strip()
    action.setProperty("guidance_reason", text)
    action.setToolTip(text)
    action.setStatusTip(text)
    action.setEnabled(False)
    if fix_callable is None:
        _FIXES.pop(id(action), None)
        action.setProperty("guidance_has_fix", False)
    else:
        _FIXES[id(action)] = fix_callable
        action.setProperty("guidance_has_fix", True)


def enable_action(action: QAction, tip: str | None = None) -> None:
    """Turn an action back on and restore its normal tip."""

    from panelcad.guidance import ACTION_TIPS

    action.setEnabled(True)
    action.setProperty("guidance_reason", "")
    _FIXES.pop(id(action), None)
    action.setProperty("guidance_has_fix", False)
    if tip is None:
        label = action.text().replace("&", "")
        found = ACTION_TIPS.get(label)
        stored = str(action.property("guidance_tip") or "")
        tip = found[0] if found else (stored or label)
    action.setToolTip(tip)
    action.setStatusTip(tip)


def disabled_reason(action: QAction) -> str:
    return str(action.property("guidance_reason") or "").strip()


def run_disabled_fix(action: QAction) -> bool:
    fn = _FIXES.get(id(action))
    if fn is None:
        return False
    fn()
    return True


def disabled_actions_missing_reason(root: QWidget) -> list[str]:
    """Labels of disabled actions that never received ``explain_disabled``."""

    missing: list[str] = []
    for action in root.findChildren(QAction):
        if action.isSeparator() or action.isEnabled():
            continue
        label = action.text().replace("&", "").strip()
        if not label:
            continue
        if not disabled_reason(action):
            missing.append(label)
    return missing


def apply_action_guidance(root: QWidget, on_hover: Callable[[QAction], None] | None = None) -> None:
    """Fill tooltips and status tips. Each tip names the in-app help topic."""

    from panelcad.guidance import ACTION_TIPS

    for action in root.findChildren(QAction):
        if action.isSeparator():
            continue
        label = action.text().replace("&", "").strip()
        if not label:
            continue
        specific = ACTION_TIPS.get(label)
        if specific is not None:
            text, topic = specific
        else:
            existing = (action.statusTip() or "").strip()
            topic = str(action.property("guidance_help") or "") or "start"
            if existing and existing != label:
                text = existing
            else:
                text = label
        if "Learn more" not in text:
            title = help_topic(str(topic))["title"]
            text = f"{text}  Learn more: {title}."
        action.setProperty("guidance_tip", text)
        action.setProperty("guidance_help", str(topic))
        if not disabled_reason(action):
            action.setToolTip(text)
            action.setStatusTip(text)
        if on_hover is not None:
            action.hovered.connect(lambda act=action: on_hover(act))


class EmptyState(QWidget):
    """One sentence and one or two buttons for an empty view."""

    clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setObjectName("muted")
        layout.addWidget(self.message)
        self._buttons = QHBoxLayout()
        layout.addLayout(self._buttons)
        self._button_widgets: list[QPushButton] = []
        self.hide()

    def set_state(self, message: str, buttons: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> None:
        text = str(message or "").strip()
        self.message.setText(text)
        while self._buttons.count():
            item = self._buttons.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._button_widgets = []
        for label, ident in buttons:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, name=ident: self.clicked.emit(name))
            self._buttons.addWidget(button)
            self._button_widgets.append(button)
        self._buttons.addStretch(1)
        self.setVisible(bool(text))

    def button(self, label: str) -> QPushButton | None:
        for button in self._button_widgets:
            if button.text() == label:
                return button
        return None


class ImportFixDialog(QDialog):
    """Name the likely file type and offer the fix, a browse path, and help."""

    def __init__(
        self,
        advice: ImportAdvice,
        error: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.advice = advice
        self.choice = ""
        self.setWindowTitle(advice.title or "Import failed")
        self.setObjectName("importFixDialog")
        layout = QVBoxLayout(self)
        self.message = QLabel(advice.message)
        self.message.setWordWrap(True)
        self.message.setObjectName("importFixMessage")
        layout.addWidget(self.message)
        if error and error.strip() and error.strip() not in advice.message:
            detail = QLabel(error.strip())
            detail.setWordWrap(True)
            detail.setObjectName("muted")
            layout.addWidget(detail)
        row = QHBoxLayout()
        self.primary = QPushButton(advice.primary_label)
        self.primary.setObjectName("importFixPrimary")
        self.primary.clicked.connect(lambda: self._choose("import"))
        row.addWidget(self.primary)
        seen = {advice.primary_label}
        self.secondary: QPushButton | None = None
        if advice.secondary_label and advice.secondary_label not in seen:
            self.secondary = QPushButton(advice.secondary_label)
            self.secondary.setObjectName("importFixSecondary")
            self.secondary.clicked.connect(lambda: self._choose("browse"))
            row.addWidget(self.secondary)
            seen.add(advice.secondary_label)
        self.help_button: QPushButton | None = None
        if advice.help_label and advice.help_label not in seen:
            self.help_button = QPushButton(advice.help_label)
            self.help_button.setObjectName("importFixHelp")
            self.help_button.clicked.connect(lambda: self._choose("help"))
            row.addWidget(self.help_button)
        layout.addLayout(row)

    def _choose(self, choice: str) -> None:
        if choice == "import" and self.advice.route == "unknown":
            choice = "help"
        self.choice = choice
        self.accept()


class HelpDialog(QDialog):
    """In-app help. Topics are the walkthrough pages, not a separate PDF."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Help")
        self.setObjectName("helpDialog")
        self.resize(640, 420)
        layout = QHBoxLayout(self)
        self.topics = QListWidget()
        self.topics.setObjectName("helpTopics")
        self._ids: list[str] = []
        for topic_id in HELP_TOPIC_ORDER:
            topic = help_topic(topic_id)
            item = QListWidgetItem(topic["title"])
            item.setData(Qt.ItemDataRole.UserRole, topic_id)
            self.topics.addItem(item)
            self._ids.append(topic_id)
        self.topics.currentRowChanged.connect(self._show_row)
        layout.addWidget(self.topics, 1)
        column = QVBoxLayout()
        self.heading = QLabel("")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        column.addWidget(self.heading)
        self.body = QTextEdit()
        self.body.setReadOnly(True)
        self.body.setObjectName("helpBody")
        column.addWidget(self.body, 1)
        layout.addLayout(column, 2)
        if self.topics.count():
            self.topics.setCurrentRow(0)

    def show_topic(self, topic_id: str) -> None:
        if topic_id in self._ids:
            self.topics.setCurrentRow(self._ids.index(topic_id))
        else:
            self._show_row(0)

    def _show_row(self, row: int) -> None:
        if row < 0 or row >= len(self._ids):
            return
        topic = help_topic(self._ids[row])
        self.heading.setText(topic["title"])
        self.body.setPlainText(topic["body"])


class HintBanner(QWidget):
    """One-time tip with Don't show again and Learn more."""

    dismissed = Signal(str)
    learn_more = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("hintBanner")
        self._hint_id = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message, 1)
        again = QPushButton("Don't show again")
        again.clicked.connect(self._again)
        learn = QPushButton("Learn more")
        learn.clicked.connect(self._learn)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        layout.addWidget(again)
        layout.addWidget(learn)
        layout.addWidget(close)
        self.hide()

    def offer(self, hint_id: str) -> bool:
        from panelcad.guidance import hint_available

        if not hint_available(hint_id):
            self.hide()
            return False
        hint = HINTS[hint_id]
        self._hint_id = hint_id
        self.message.setText(hint["text"])
        self.show()
        return True

    def _again(self) -> None:
        from panelcad.guidance import dismiss_hint

        if self._hint_id:
            dismiss_hint(self._hint_id)
            self.dismissed.emit(self._hint_id)
        self.hide()

    def _learn(self) -> None:
        if not self._hint_id:
            return
        self.learn_more.emit(HINTS[self._hint_id]["topic"])


class NextStepsPanel(QWidget):
    """Checklist driven by project state. Recompute is debounced and skipped while hidden."""

    do_requested = Signal(str)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("nextStepsPanel")
        self._session = None
        self._problem_count = 0
        self.recomputes = 0
        self.schedules = 0
        self._dirty = False
        self._buttons: dict[str, QPushButton] = {}
        self._marks: dict[str, QLabel] = {}
        self._details: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        title = QLabel("Next steps")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("nextStepsSummary")
        layout.addWidget(self.summary)
        scroller = QScrollArea()
        scroller.setObjectName("nextStepsScroll")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setMinimumHeight(96)
        scroller.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        host = QWidget()
        steps = QVBoxLayout(host)
        steps.setContentsMargins(0, 0, 0, 0)
        for step_id in STEP_ORDER:
            row = QHBoxLayout()
            mark = QLabel("○")
            mark.setMinimumWidth(16)
            detail = QLabel(step_id)
            detail.setWordWrap(True)
            button = QPushButton("Do it")
            button.setObjectName(f"nextStep_{step_id}")
            button.clicked.connect(lambda _checked=False, name=step_id: self.do_requested.emit(name))
            row.addWidget(mark)
            row.addWidget(detail, 1)
            row.addWidget(button)
            steps.addLayout(row)
            self._marks[step_id] = mark
            self._details[step_id] = detail
            self._buttons[step_id] = button
        scroller.setWidget(host)
        layout.addWidget(scroller, 1)
        dismiss = QPushButton("Dismiss")
        dismiss.setObjectName("nextStepsDismiss")
        dismiss.clicked.connect(self._dismiss)
        layout.addWidget(dismiss)
        layout.addStretch(1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(NEXT_STEPS_DEBOUNCE_MS)
        self._timer.timeout.connect(self._recompute)

    def set_session(self, session) -> None:
        self._session = session
        self.schedule()

    def set_problem_count(self, count: int) -> None:
        self._problem_count = int(count)

    def schedule(self) -> None:
        """Queue one recompute. Hidden panels wait until they are shown."""

        self.schedules += 1
        if not self.isVisible():
            self._dirty = True
            self._timer.stop()
            return
        self._dirty = False
        self._timer.start()

    def recompute_now(self) -> None:
        self._timer.stop()
        self._recompute()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._dirty:
            self.schedule()

    def button(self, step_id: str) -> QPushButton:
        return self._buttons[step_id]

    def apply_steps(self, steps: list[ChecklistStep]) -> None:
        needs = ""
        for step in steps:
            mark = self._marks.get(step.id)
            detail = self._details.get(step.id)
            if mark is not None:
                mark.setText("✓" if step.done else "○")
            if detail is not None:
                detail.setText(f"{step.title}: {step.detail}")
            if step.id == "parts" and "NOT FOR BUILD" in step.detail:
                needs = step.detail
            if step.id == "print" and "NOT FOR BUILD" in step.detail:
                needs = step.detail
        self.summary.setText(needs)

    def _recompute(self) -> None:
        from panelcad.guidance import project_checklist

        self.recomputes += 1
        self._dirty = False
        if self._session is None:
            return
        self.apply_steps(project_checklist(self._session, problem_count=self._problem_count))

    def _dismiss(self) -> None:
        from panelcad.guidance import set_next_steps_open

        set_next_steps_open(False)
        self.dismissed.emit()


def _callout_style(object_name: str) -> str:
    from panelcad.theme import ui_hex

    return (
        f"QFrame#{object_name} {{ background: {ui_hex('banner_info_bg')};"
        f" color: {ui_hex('banner_info_fg')}; border: 1px solid {ui_hex('border')};"
        f" border-radius: 6px; }}"
        f"QFrame#{object_name} QLabel {{ background: transparent; color: {ui_hex('banner_info_fg')}; }}"
    )


def annotate(widget: QWidget, key: str) -> None:
    """Attach a what's-this note. An existing tooltip is left as it is."""

    from panelcad.learn import whats_this

    info = whats_this(key)
    if info is None:
        return
    widget.setProperty("guidance_key", key)
    widget.setWhatsThis(info["text"])
    if not (widget.toolTip() or "").strip():
        widget.setToolTip(info["text"])


def describe_widget(widget: QWidget | None) -> dict[str, str] | None:
    """Walk the widget and its parents for a what's-this note."""

    from panelcad.learn import whats_this

    seen: set[int] = set()
    current = widget
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        key = str(current.property("guidance_key") or "")
        if key:
            found = whats_this(key)
            if found is not None:
                return found
        name = current.objectName()
        if name:
            found = whats_this(name)
            if found is not None:
                return found
        label = ""
        if isinstance(current, QToolButton):
            action = current.defaultAction()
            if action is not None:
                label = action.text().replace("&", "").strip()
        elif isinstance(current, QAbstractButton):
            label = current.text().replace("&", "").strip()
        elif isinstance(current, QDockWidget):
            label = current.windowTitle().strip()
        if label:
            found = whats_this(label)
            if found is not None:
                return found
        current = current.parentWidget()
    return None


class InlineNote(QFrame):
    """A short note on a dialog. Don't show again stores the id in settings."""

    dismissed = Signal(str)

    def __init__(self, note_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.note_id = note_id
        self.setObjectName("inlineGuidance")
        self.setStyleSheet(_callout_style("inlineGuidance"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        self.heading = QLabel("")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setObjectName("inlineGuidanceBody")
        layout.addWidget(self.body)
        row = QHBoxLayout()
        again = QPushButton("Don't show again")
        again.setObjectName("inlineGuidanceDismiss")
        again.clicked.connect(self._dismiss)
        row.addWidget(again)
        row.addStretch(1)
        layout.addLayout(row)
        self.hide()

    def set_copy(self, title: str, body: str) -> None:
        self.heading.setText(title)
        self.body.setText(body)
        self.refresh()

    def refresh(self) -> None:
        from panelcad.guidance import note_dismissed

        if note_dismissed(self.note_id) or not self.body.text().strip():
            self.hide()
            return
        self.show()

    def _dismiss(self) -> None:
        from panelcad.guidance import dismiss_note

        dismiss_note(self.note_id)
        self.hide()
        self.dismissed.emit(self.note_id)


class WelcomeDialog(QDialog):
    """First-run welcome. Closing it can leave the welcome pending."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle("Welcome to PanelCAD")
        self.setObjectName("welcomeDialog")
        self.resize(520, 320)
        layout = QVBoxLayout(self)
        heading = QLabel("Welcome to PanelCAD")
        font = heading.font()
        font.setPointSize(heading.font().pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        body = QLabel(
            "PanelCAD is the drawing for an industrial control panel. "
            "A short tour shows the Library, the sheet, Wire, the Inspector, "
            "the BOM, print, and Troubleshoot.\n\n"
            "Task checklists cover a new project, placing parts, wiring, "
            "the BOM and quote, print, CSV import, print import, and troubleshooting. "
            "Press F1 later for the same how-tos. Nothing here needs a network or an AI account.\n\n"
            "Start drawing hides this welcome. Help → Welcome opens it again. "
            "The choice is saved in your settings, not in the drawing."
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        self.dont_again = QCheckBox("Don't show this welcome again")
        self.dont_again.setObjectName("welcomeDontAgain")
        self.dont_again.setChecked(True)
        layout.addWidget(self.dont_again)
        row = QHBoxLayout()
        self.tour_button = QPushButton("Take the tour")
        self.tour_button.setObjectName("welcomeTour")
        self.tour_button.setDefault(True)
        self.tour_button.clicked.connect(lambda: self._choose("tour"))
        self.help_button = QPushButton("Open how-tos")
        self.help_button.setObjectName("welcomeHelp")
        self.help_button.clicked.connect(lambda: self._choose("help"))
        self.start_button = QPushButton("Start drawing")
        self.start_button.setObjectName("welcomeStart")
        self.start_button.clicked.connect(lambda: self._choose("start"))
        row.addWidget(self.tour_button)
        row.addWidget(self.help_button)
        row.addWidget(self.start_button)
        layout.addLayout(row)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


class TourCard(QFrame):
    """One tour stop. Hide pauses. Skip dismisses. Both can be resumed from Help."""

    advance = Signal(int)
    pause = Signal()
    skip = Signal()
    _CARD_WIDTH = 360
    _MIN_CARD_WIDTH = 180

    def __init__(self, parent: QWidget | None = None) -> None:
        # On Windows a parented top-level window (Tool or Frameless) is placed
        # in parent-client coordinates while frameGeometry() is global, so a
        # card aimed at a 1024x700 window lands outside it. A plain child is
        # positioned in that client rect. Other platforms keep a tool window
        # so the card stays above the docks.
        if sys.platform == "win32":
            super().__init__(parent)
        else:
            flags = (
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.NoDropShadowWindowHint
            )
            super().__init__(parent, flags)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("tourCard")
        self.setStyleSheet(_callout_style("tourCard"))
        self.resize(360, 220)
        self._index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.progress = QLabel("")
        self.progress.setObjectName("tourProgress")
        layout.addWidget(self.progress)
        self.heading = QLabel("")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setObjectName("tourBody")
        layout.addWidget(self.body)
        self.back = QPushButton("Back")
        self.back.setObjectName("tourBack")
        self.back.clicked.connect(lambda: self.advance.emit(-1))
        self.next = QPushButton("Next")
        self.next.setObjectName("tourNext")
        self.next.clicked.connect(lambda: self.advance.emit(1))
        self.hide_button = QPushButton("Hide")
        self.hide_button.setObjectName("tourHide")
        self.hide_button.clicked.connect(self.pause.emit)
        self.skip_button = QPushButton("Skip tour")
        self.skip_button.setObjectName("tourSkip")
        self.skip_button.clicked.connect(self.skip.emit)
        self._buttons = (self.back, self.next, self.hide_button, self.skip_button)
        self._button_grid = QGridLayout()
        self._button_grid.setContentsMargins(0, 0, 0, 0)
        self._buttons_wrapped = None
        layout.addLayout(self._button_grid)
        self._place_buttons(wrapped=False)
        self.hide()

    def _place_buttons(self, wrapped: bool) -> None:
        """One row at full width. Two rows when the card has been narrowed."""

        if wrapped == self._buttons_wrapped:
            return
        self._buttons_wrapped = wrapped
        for button in self._buttons:
            self._button_grid.removeWidget(button)
        if wrapped:
            for button in self._buttons:
                button.setMinimumWidth(0)
            for index, button in enumerate(self._buttons):
                self._button_grid.addWidget(button, index // 2, index % 2)
        else:
            for index, button in enumerate(self._buttons):
                self._button_grid.addWidget(button, 0, index)

    def set_step(self, index: int) -> None:
        from panelcad.learn import TOUR_STEPS

        self._index = index
        step = TOUR_STEPS[index]
        self.progress.setText(f"Tour {index + 1} of {len(TOUR_STEPS)}")
        self.heading.setText(step["title"])
        self.body.setText(step["body"])
        self.back.setEnabled(index > 0)
        self.next.setText("Done" if index == len(TOUR_STEPS) - 1 else "Next")

    def place_near(self, anchor: QWidget | None) -> None:
        """Put the card beside the anchor, inside the window and the screen.

        A right-hand dock gets the card on its left. The toolbar gets it below.
        The sheet is large, so that stop sits inside the canvas. Every other
        stop uses whichever side has room for the card.
        """

        bounds = self._placement_bounds()
        self._fit_card(bounds)
        anchor_rect = self._anchor_rect(anchor, bounds)
        if anchor_rect is None:
            width = self.width()
            height = self.height()
            x = bounds.left() + max(0, (bounds.width() - width) // 2)
            y = bounds.top() + max(0, (bounds.height() - height) // 2)
        else:
            if self._narrow_to_free_side(anchor, anchor_rect, bounds):
                x, y = self._point_beside(
                    anchor, anchor_rect, bounds, self.width(), self.height()
                )
            else:
                x, y = self._place_least_overlap(anchor_rect, bounds)
        self._move_inside(x, y, bounds)
        self.show()
        self.raise_()

    def _placement_bounds(self) -> QRect:
        parent = self.parentWidget()
        window = parent.window() if parent is not None else None
        screen = window.screen() if window is not None else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect()
        if not self.isWindow() and parent is not None:
            # Parent client coordinates. rect() is the 1024x700 area, not the
            # frame that hangs outside the work area when the window is maximized.
            local = parent.rect()
            if not local.isValid():
                return QRect(0, 0, 800, 600)
            if not available.isValid():
                return local
            global_client = QRect(parent.mapToGlobal(local.topLeft()), local.size())
            both = global_client.intersected(available)
            if not both.isValid():
                return local
            origin = parent.mapFromGlobal(both.topLeft())
            return QRect(origin, both.size())
        client = window.frameGeometry() if window is not None and window.isVisible() else QRect()
        if client.isValid() and available.isValid():
            both = client.intersected(available)
            if both.isValid():
                return both
        if client.isValid():
            return client
        if available.isValid():
            return available
        return QRect(0, 0, 800, 600)

    def _anchor_rect(self, anchor: QWidget | None, bounds: QRect) -> QRect | None:
        if anchor is None or not anchor.isVisible():
            return None
        rect = self._widget_rect(anchor)
        if rect.intersects(bounds):
            return rect
        # A tabbed dock that is not the current page keeps a stale geometry
        # outside the window. Use the visible tab in that group.
        if isinstance(anchor, QDockWidget):
            parent = self.parentWidget()
            window = parent.window() if parent is not None else None
            if window is not None:
                for other in window.tabifiedDockWidgets(anchor):
                    if not other.isVisible():
                        continue
                    other_rect = self._widget_rect(other)
                    if other_rect.intersects(bounds):
                        return other_rect
        return rect

    def _widget_rect(self, widget: QWidget) -> QRect:
        if self.isWindow():
            return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
        parent = self.parentWidget()
        origin = parent.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
        return QRect(origin, widget.size())

    def _move_frame_to(self, x: int, y: int) -> None:
        """Put ``frameGeometry``'s top-left at the global point ``(x, y)``.

        On Windows, ``move`` and ``frameGeometry`` can disagree by the window
        frame. Shifting the client rect by the measured error lands the frame.
        """

        self.move(int(x), int(y))
        for _ in range(3):
            frame = self.frameGeometry()
            dx = int(x) - int(frame.x())
            dy = int(y) - int(frame.y())
            if dx == 0 and dy == 0:
                return
            client = self.geometry()
            self.setGeometry(client.x() + dx, client.y() + dy, client.width(), client.height())

    def _fit_card(self, bounds: QRect, width: int | None = None) -> None:
        if width is None:
            width = min(self._CARD_WIDTH, max(1, bounds.width()))
        width = max(1, min(int(width), max(1, bounds.width())))
        self.setFixedWidth(width)
        self._place_buttons(width < self._CARD_WIDTH)
        inner = max(1, width - 28)
        self.body.setMaximumWidth(inner)
        self.heading.setMaximumWidth(inner)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        height = min(max(1, self.sizeHint().height()), max(1, bounds.height()))
        self.setMaximumHeight(max(1, bounds.height()))
        self.resize(width, height)

    def _narrow_to_free_side(self, anchor: QWidget, anchor_rect: QRect, bounds: QRect) -> bool:
        """Shrink the card when neither side can hold the full width.

        Returns False when no side, including above and below, can hold the
        card. Sheet and toolbar stops stay on their existing path.
        """

        if anchor.objectName() in {"sheetCanvas", "mainToolbar"}:
            return True
        gap = 8
        width = self.width()
        height = self.height()
        space_left = anchor_rect.left() - bounds.left()
        space_right = bounds.right() - anchor_rect.right()
        space_above = anchor_rect.top() - bounds.top()
        space_below = bounds.bottom() - anchor_rect.bottom()
        if (
            space_left - gap >= width
            or space_right - gap >= width
            or space_above >= height
            or space_below >= height
        ):
            return True
        room = max(space_left, space_right) - gap
        if room >= self._MIN_CARD_WIDTH:
            self._fit_card(bounds, width=room)
            return True
        return False

    def _place_least_overlap(self, anchor_rect: QRect, bounds: QRect) -> tuple[int, int]:
        """Put the card where it covers the anchor least, preferring the sheet."""

        gap = 8
        self._fit_card(bounds, width=min(self._MIN_CARD_WIDTH, max(1, bounds.width())))
        width = self.width()
        height = self.height()

        def clamp(x: int, y: int) -> tuple[int, int]:
            x = max(bounds.left(), min(int(x), bounds.right() - width + 1))
            y = max(bounds.top(), min(int(y), bounds.bottom() - height + 1))
            return x, y

        def overlap(x: int, y: int) -> int:
            hit = QRect(x, y, width, height).intersected(anchor_rect)
            if not hit.isValid():
                return 0
            return max(0, hit.width()) * max(0, hit.height())

        candidates: list[tuple[int, int, int, int]] = []

        def consider(x: int, y: int, on_sheet: bool) -> None:
            x, y = clamp(x, y)
            candidates.append((overlap(x, y), 0 if on_sheet else 1, x, y))

        consider(anchor_rect.right() + 1 + gap, bounds.top() + gap, False)
        consider(anchor_rect.left() - gap - width, bounds.top() + gap, False)
        consider(bounds.left() + gap, anchor_rect.top() - gap - height, False)
        consider(bounds.left() + gap, anchor_rect.bottom() + 1 + gap, False)
        sheet = self._sheet_rect(bounds)
        if sheet is not None:
            consider(sheet.left() + gap, sheet.top() + gap, True)
            consider(sheet.right() - width + 1, sheet.top() + gap, True)
            consider(sheet.left() + gap, sheet.bottom() - height + 1, True)
        if not candidates:
            return bounds.left(), bounds.top()
        _area, _rank, x, y = min(candidates)
        return x, y

    def _sheet_rect(self, bounds: QRect) -> QRect | None:
        parent = self.parentWidget()
        window = parent.window() if parent is not None else None
        if window is None:
            return None
        sheet = window.findChild(QWidget, "sheetCanvas")
        if sheet is None or not sheet.isVisible():
            return None
        rect = self._widget_rect(sheet).intersected(bounds)
        if not rect.isValid() or rect.width() < 8 or rect.height() < 8:
            return None
        return rect

    def _point_beside(
        self,
        anchor: QWidget,
        anchor_rect: QRect,
        bounds: QRect,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        gap = 8
        name = anchor.objectName()
        if name == "sheetCanvas":
            # The sheet is the drawing, so the card sits on it. Shift only when
            # the canvas is large enough to hold the whole card.
            visible = anchor_rect.intersected(bounds)
            if not visible.isValid():
                visible = anchor_rect if anchor_rect.isValid() else bounds
            x = visible.left() + gap
            y = visible.top() + gap
            if visible.width() >= width and x + width - 1 > visible.right():
                x = visible.right() - width + 1
            if visible.height() >= height and y + height - 1 > visible.bottom():
                y = visible.bottom() - height + 1
            return x, y
        if name == "mainToolbar":
            return anchor_rect.left() + gap, anchor_rect.bottom() + 1 + gap

        space_left = anchor_rect.left() - bounds.left()
        space_right = bounds.right() - anchor_rect.right()
        space_above = anchor_rect.top() - bounds.top()
        space_below = bounds.bottom() - anchor_rect.bottom()
        # A side that cannot hold the minimum card is not a candidate, so the
        # clamp cannot drag a too-wide card back over the anchor.
        left_score = space_left if space_left - gap >= width else -1
        right_score = space_right if space_right - gap >= width else -1
        options = (
            (left_score >= 0, left_score, anchor_rect.left() - gap - width, anchor_rect.top()),
            (right_score >= 0, right_score, anchor_rect.right() + 1 + gap, anchor_rect.top()),
            (
                space_below >= height,
                space_below,
                anchor_rect.left(),
                anchor_rect.bottom() + 1 + gap,
            ),
            (
                space_above >= height,
                space_above,
                anchor_rect.left(),
                anchor_rect.top() - gap - height,
            ),
        )
        _fits, _space, x, y = max(options, key=lambda item: (item[0], item[1]))
        return x, y

    def _move_inside(self, x: int, y: int, bounds: QRect) -> None:
        self._move_frame_to(int(x), int(y))
        for _ in range(4):
            frame = self.frameGeometry()
            extra_w = frame.width() - self.width()
            extra_h = frame.height() - self.height()
            if frame.width() > bounds.width() or frame.height() > bounds.height():
                self.resize(
                    max(1, bounds.width() - max(0, extra_w)),
                    max(1, bounds.height() - max(0, extra_h)),
                )
                frame = self.frameGeometry()
            dx = 0
            dy = 0
            if frame.left() < bounds.left():
                dx = bounds.left() - frame.left()
            elif frame.right() > bounds.right():
                dx = bounds.right() - frame.right()
            if frame.top() < bounds.top():
                dy = bounds.top() - frame.top()
            elif frame.bottom() > bounds.bottom():
                dy = bounds.bottom() - frame.bottom()
            if dx == 0 and dy == 0:
                return
            self._move_frame_to(frame.x() + dx, frame.y() + dy)


class HelpPanel(QWidget):
    """Searchable how-tos and the task checklists. F1 shows the dock that holds this."""

    action_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpPanel")
        self._topic_ids: list[str] = []
        self._task_id = ""
        self._checks: list[QCheckBox] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find"))
        self.search = QLineEdit()
        self.search.setObjectName("helpSearch")
        self.search.setPlaceholderText("Search how-tos")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._research)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)
        columns = QHBoxLayout()
        self.topics = QListWidget()
        self.topics.setObjectName("helpPanelTopics")
        self.topics.currentRowChanged.connect(self._show_row)
        columns.addWidget(self.topics, 1)
        text = QVBoxLayout()
        self.heading = QLabel("")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        text.addWidget(self.heading)
        self.body = QTextEdit()
        self.body.setReadOnly(True)
        self.body.setObjectName("helpPanelBody")
        text.addWidget(self.body, 1)
        columns.addLayout(text, 2)
        layout.addLayout(columns, 1)
        tasks_head = QLabel("Task checklists")
        tasks_font = tasks_head.font()
        tasks_font.setBold(True)
        tasks_head.setFont(tasks_font)
        layout.addWidget(tasks_head)
        task_row = QHBoxLayout()
        self.task_combo = QComboBox()
        self.task_combo.setObjectName("taskCombo")
        from panelcad.learn import TASK_ORDER, TASKS

        for task_id in TASK_ORDER:
            self.task_combo.addItem(str(TASKS[task_id]["title"]), task_id)
        self.task_combo.currentIndexChanged.connect(self._task_changed)
        task_row.addWidget(self.task_combo, 1)
        self.task_hide = QPushButton("Hide checklist")
        self.task_hide.setObjectName("taskHide")
        self.task_hide.clicked.connect(lambda: self._set_task_hidden(True))
        self.task_show = QPushButton("Show again")
        self.task_show.setObjectName("taskShow")
        self.task_show.clicked.connect(lambda: self._set_task_hidden(False))
        task_row.addWidget(self.task_hide)
        task_row.addWidget(self.task_show)
        layout.addLayout(task_row)
        self.task_summary = QLabel("")
        self.task_summary.setWordWrap(True)
        self.task_summary.setObjectName("taskSummary")
        layout.addWidget(self.task_summary)
        self.task_hidden = QLabel("This checklist is hidden. Show again puts the steps back.")
        self.task_hidden.setWordWrap(True)
        self.task_hidden.setObjectName("taskHiddenNote")
        layout.addWidget(self.task_hidden)
        self.task_scroll = QScrollArea()
        self.task_scroll.setObjectName("taskScroll")
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.task_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.task_scroll.setMinimumHeight(260)
        self.task_body = QWidget()
        self.task_body.setObjectName("taskBody")
        self.task_host = QVBoxLayout(self.task_body)
        self.task_host.setContentsMargins(0, 0, 0, 0)
        self.task_scroll.setWidget(self.task_body)
        layout.addWidget(self.task_scroll, 2)
        self._research()
        self._task_changed()

    def show_topic(self, topic_id: str) -> None:
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._fill_topics("")
        if topic_id in self._topic_ids:
            self.topics.setCurrentRow(self._topic_ids.index(topic_id))
        elif self.topics.count():
            self.topics.setCurrentRow(0)

    def show_tasks(self, task_id: str = "") -> None:
        from panelcad.learn import TASK_ORDER

        target = task_id if task_id in TASK_ORDER else (TASK_ORDER[0] if TASK_ORDER else "")
        if target:
            index = self.task_combo.findData(target)
            if index >= 0:
                self.task_combo.setCurrentIndex(index)
        self._task_changed()

    def current_topic(self) -> str:
        row = self.topics.currentRow()
        if row < 0 or row >= len(self._topic_ids):
            return ""
        return self._topic_ids[row]

    def _research(self) -> None:
        self._fill_topics(self.search.text())
        self._filter_tasks(self.search.text())

    def _fill_topics(self, query: str) -> None:
        from panelcad.guidance import help_topic, search_help

        chosen = self.current_topic()
        ids = search_help(query)
        self._topic_ids = ids
        self.topics.blockSignals(True)
        self.topics.clear()
        for topic_id in ids:
            topic = help_topic(topic_id)
            item = QListWidgetItem(topic["title"])
            item.setData(Qt.ItemDataRole.UserRole, topic_id)
            self.topics.addItem(item)
        self.topics.blockSignals(False)
        if not ids:
            self.heading.setText("No matching how-to")
            self.body.setPlainText("Nothing in Help matches that search. Try a job name, such as CSV or wire.")
            return
        if chosen in ids:
            self.topics.setCurrentRow(ids.index(chosen))
        else:
            self.topics.setCurrentRow(0)

    def _show_row(self, row: int) -> None:
        from panelcad.guidance import help_topic

        if row < 0 or row >= len(self._topic_ids):
            return
        topic = help_topic(self._topic_ids[row])
        self.heading.setText(topic["title"])
        self.body.setPlainText(topic["body"])

    def _filter_tasks(self, query: str) -> None:
        from panelcad.guidance import search_tasks

        matches = search_tasks(query)
        if not matches:
            return
        current = str(self.task_combo.currentData() or "")
        if current in matches:
            return
        index = self.task_combo.findData(matches[0])
        if index >= 0:
            self.task_combo.setCurrentIndex(index)

    def _task_changed(self, _index: int = 0) -> None:
        self._task_id = str(self.task_combo.currentData() or "")
        self._rebuild_task()

    def _set_task_hidden(self, hidden: bool) -> None:
        from panelcad.guidance import set_task_dismissed

        if not self._task_id:
            return
        set_task_dismissed(self._task_id, hidden)
        self._rebuild_task()

    def _rebuild_task(self) -> None:
        from panelcad.guidance import task_record
        from panelcad.learn import TASKS

        while self.task_host.count():
            item = self.task_host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._checks = []
        task = TASKS.get(self._task_id)
        if task is None:
            self.task_summary.setText("")
            self.task_hidden.hide()
            return
        record = task_record(self._task_id)
        checked = set(record["checked"])
        hidden = bool(record["dismissed"])
        self.task_summary.setText(str(task["summary"]))
        self.task_hidden.setVisible(hidden)
        self.task_hide.setVisible(not hidden)
        self.task_show.setVisible(hidden)
        self.task_scroll.setVisible(not hidden)
        if hidden:
            return
        from panelcad.learn import task_button_label

        for step in task["steps"]:  # type: ignore[union-attr]
            step_id = str(step["id"])
            row_host = QWidget()
            row_host.setObjectName(f"taskRow_{self._task_id}_{step_id}")
            outer = QVBoxLayout(row_host)
            outer.setContentsMargins(0, 4, 0, 4)
            outer.setSpacing(2)
            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            box = QCheckBox(str(step["title"]))
            box.setObjectName(f"task_{self._task_id}_{step_id}")
            box.setChecked(step_id in checked)
            box.setToolTip(str(step["detail"]))
            box.toggled.connect(lambda state, name=step_id: self._toggle_step(name, state))
            title_row.addWidget(box, 1)
            action = str(step.get("action") or "")
            button_height = 0
            if action:
                button = QPushButton(task_button_label(action))
                button.setObjectName(f"taskDo_{self._task_id}_{step_id}")
                button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                button.setMinimumSize(button.sizeHint())
                button.clicked.connect(
                    lambda _checked=False, name=action, step_name=step_id: self._do(step_name, name)
                )
                title_row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
                button_height = button.minimumHeight()
            outer.addLayout(title_row)
            detail = QLabel(str(step["detail"]))
            detail.setWordWrap(True)
            detail.setObjectName(f"taskDetail_{self._task_id}_{step_id}")
            detail.setMinimumHeight(detail.sizeHint().height())
            outer.addWidget(detail)
            row_host.setMinimumHeight(
                max(button_height, box.sizeHint().height()) + detail.minimumHeight() + 12
            )
            row_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            self.task_host.addWidget(row_host)
            self._checks.append(box)

    def _toggle_step(self, step_id: str, checked: bool) -> None:
        from panelcad.guidance import set_task_step

        if not self._task_id:
            return
        set_task_step(self._task_id, step_id, bool(checked))

    def _do(self, step_id: str, action: str) -> None:
        from panelcad.guidance import set_task_step

        if self._task_id:
            set_task_step(self._task_id, step_id, True)
        self.action_requested.emit(action)
        self._rebuild_task()


class WhatsThisPopup(QFrame):
    """One-shot note for the control that was clicked."""

    learn_more = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("whatsThisPopup")
        self.setStyleSheet(_callout_style("whatsThisPopup"))
        self._topic = "start"
        self.resize(320, 160)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.heading = QLabel("")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setObjectName("whatsThisBody")
        layout.addWidget(self.body)
        row = QHBoxLayout()
        learn = QPushButton("Learn more")
        learn.setObjectName("whatsThisLearn")
        learn.clicked.connect(lambda: self.learn_more.emit(self._topic))
        close = QPushButton("Close")
        close.setObjectName("whatsThisClose")
        close.clicked.connect(self.hide)
        row.addWidget(learn)
        row.addWidget(close)
        row.addStretch(1)
        layout.addLayout(row)
        self.hide()

    def show_message(self, title: str, text: str, topic: str) -> None:
        self._topic = topic or "start"
        self.heading.setText(title)
        self.body.setText(text)
        self.adjustSize()
        self.show()
        self.raise_()

    def place_near(self, anchor: QWidget | None) -> None:
        self.adjustSize()
        if anchor is None or not anchor.isVisible():
            parent = self.parentWidget()
            if parent is not None and parent.isVisible():
                center = parent.mapToGlobal(parent.rect().center())
                self.move(center.x() - self.width() // 2, max(0, center.y() - self.height() // 2))
            return
        origin = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self.move(origin + QPoint(8, 8))


class WhatsThisController(QObject):
    """Shift+F1, then one click. Esc cancels. The click is not delivered to the control."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.window = window
        self.active = False
        self.popup = WhatsThisPopup(window)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def shutdown(self, *_args) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.active = False

    def enter(self) -> None:
        self.active = True
        self.popup.hide()

    def leave(self) -> None:
        self.active = False

    def show_for(self, widget: QWidget) -> None:
        info = describe_widget(widget)
        self.leave()
        if info is None:
            self.popup.show_message(
                "What's this",
                "No extra note for this control. Press F1 for the how-tos.",
                "start",
            )
        else:
            self.popup.show_message(info["title"], info["text"], info.get("topic") or "start")
        self.popup.place_near(widget)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if not self.active:
            return False
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            target = watched if isinstance(watched, QWidget) else None
            if target is None:
                return False
            if (
                target is not self.window
                and not self.window.isAncestorOf(target)
                and target is not self.popup
                and not self.popup.isAncestorOf(target)
            ):
                return False
            self.leave()
            return True
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None:
            return False
        if widget is self.popup or self.popup.isAncestorOf(widget):
            return False
        if widget is not self.window and not self.window.isAncestorOf(widget):
            return False
        self.show_for(widget)
        return True


class CsvImportDialog(QDialog):
    """Parts-list CSV. The column guide stays. The longer note can be dismissed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from panelcad.learn import csv_import_note, csv_import_summary

        self.setWindowTitle("Import a vendor parts list")
        self.setObjectName("csvImportDialog")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        note = csv_import_note()
        self.note = InlineNote("csv-import")
        self.note.set_copy(note["title"], note["body"])
        layout.addWidget(self.note)
        self.summary = QLabel(csv_import_summary())
        self.summary.setObjectName("csvImportSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        form = QHBoxLayout()
        form.addWidget(QLabel("File"))
        self.path = QLineEdit()
        self.path.setObjectName("csvImportPath")
        annotate(self.path, "csvImportPath")
        self.path.textChanged.connect(self._refresh_import)
        form.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("csvImportBrowse")
        browse.clicked.connect(self._browse)
        form.addWidget(browse)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.help_button = QPushButton("How to import a CSV")
        self.help_button.setObjectName("csvImportHelp")
        self.help_button.clicked.connect(self._help)
        self.import_button = QPushButton("Import")
        self.import_button.setObjectName("csvImportGo")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.help_button)
        buttons.addStretch(1)
        buttons.addWidget(self.import_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.help_wanted = False

    def _refresh_import(self) -> None:
        self.import_button.setEnabled(bool(self.path.text().strip()))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import BOM", "", "CSV (*.csv);;All files (*.*)")
        if path:
            self.path.setText(path)

    def _help(self) -> None:
        self.help_wanted = True
        self.reject()

    def path_text(self) -> str:
        return self.path.text().strip()
