"""Help → Report a bug, the crash dialog, next-start recovery, and Settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.diag.crash import (
    UncleanExit,
    acknowledge_unclean,
    load_crash,
    plain_when,
    recovery_sentence,
)
from panelcad.diag.report import (
    ReportRequest,
    preview_text,
    recent_actions_text,
    traceback_summary,
)
from panelcad.diag.settings import destination_line

TEST_TIP = "Add a webhook, or a bot token and a channel id."


def grab_png(widget: QWidget | None) -> bytes | None:
    if widget is None:
        return None
    try:
        pix = widget.grab()
        if pix.isNull() or pix.width() < 2:
            return None
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pix.save(buf, "PNG"):
            return None
        data = bytes(buf.data())
        buf.close()
        return data or None
    except Exception:  # noqa: BLE001
        return None


def screen_work_area(widget: QWidget | None = None) -> QRect:
    """Available geometry of the widget's screen, including its origin.

    Tests replace this. ``screen_limit`` is only the width and height.
    """

    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QRect(0, 0, 1366, 768)
    return screen.availableGeometry()


def screen_limit(widget: QWidget | None = None) -> tuple[int, int]:
    """Available screen size. Tests replace this to imitate a laptop or a desktop."""

    rect = screen_work_area(widget)
    return max(int(rect.width()), 1), max(int(rect.height()), 1)


def _place_dialog(dialog: QDialog) -> None:
    """Move ``dialog`` so its frame sits inside the current screen's work area."""

    area = screen_work_area(dialog)
    sw, sh = screen_limit(dialog)
    box = QRect(int(area.x()), int(area.y()), int(sw), int(sh))
    frame = dialog.frameGeometry()
    extra_h = max(0, frame.height() - dialog.height())
    extra_w = max(0, frame.width() - dialog.width())
    if extra_h or extra_w:
        fitted_w = min(dialog.width(), max(420, box.width() - extra_w))
        fitted_h = min(dialog.height(), max(280, box.height() - extra_h))
        if fitted_w != dialog.width() or fitted_h != dialog.height():
            dialog.resize(fitted_w, fitted_h)
            frame = dialog.frameGeometry()
    x, y, w, h = frame.x(), frame.y(), frame.width(), frame.height()
    if w > box.width():
        x = box.x()
    else:
        x = min(max(x, box.x()), box.x() + box.width() - w)
    if h > box.height():
        y = box.y()
    else:
        y = min(max(y, box.y()), box.y() + box.height() - h)
    dialog.move(dialog.x() + (x - frame.x()), dialog.y() + (y - frame.y()))


def _layout_height(dialog: QDialog, width: int) -> int:
    """Height the layout needs at ``width``, including wrapped labels.

    ``sizeHint`` is often the height at a different width, so a resize to that
    hint is shorter than the wrapped text and Qt logs "Unable to set geometry".
    """

    hint = max(dialog.sizeHint().height(), 1)
    layout = dialog.layout()
    if layout is None or not layout.hasHeightForWidth():
        return hint
    try:
        measured = int(layout.totalHeightForWidth(width))
    except (TypeError, ValueError):
        return hint
    if measured <= 0:
        return hint
    return max(measured, hint)


def clamp_dialog(dialog: QDialog, *, width: int) -> None:
    """Keep a dialog inside the screen. Extra text scrolls inside the dialog."""

    sw, sh = screen_limit(dialog)
    max_w = min(width, max(420, sw - 48))
    max_h = max(280, sh - 48)
    dialog.setMaximumSize(max_w, max_h)
    dialog.setMinimumSize(0, 0)
    layout = dialog.layout()
    if layout is not None:
        # The layout's own minimum is the wrapped height at the narrowest width.
        # Asking for the shorter height at the real width then logs
        # "Unable to set geometry" on Windows.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.invalidate()
        layout.activate()
    height = min(_layout_height(dialog, max_w), max_h)
    dialog.resize(max_w, height)
    _place_dialog(dialog)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wrapping(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _ElidedPath(QLabel):
    """One line, middle-elided. The full path stays in the tooltip and can be copied from Details."""

    def __init__(self, full: str, parent: QWidget | None = None, *, object_name: str = "") -> None:
        super().__init__(parent)
        self._full = str(full or "")
        if object_name:
            self.setObjectName(object_name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWordWrap(False)
        self.setToolTip(self._full)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(self._full)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        width = max(40, self.contentsRect().width())
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, width))


def _open_folder(path: str) -> None:
    folder = str(Path(path).parent)
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def _heading(text: str, object_name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setStyleSheet("font-weight: 600;")
    return label


def _send_notice(result: dict) -> tuple[bool, str]:
    from panelcad.diag.reporters import delivery_message

    send = result.get("send") if isinstance(result.get("send"), dict) else {}
    path = str(result.get("path") or "")
    text = str(send.get("message") or "")
    if not text:
        text = delivery_message(send, path)
    return bool(send.get("ok")), text


def _recovery_details(info: UncleanExit) -> str:
    lines: list[str] = []
    if info.reasons:
        lines.append("Markers: " + ", ".join(info.reasons))
    if info.crash_id:
        lines.append(f"Crash id: {info.crash_id}")
    if info.crash_path:
        lines.append(f"Crash record: {info.crash_path}")
    if info.project_path:
        lines.append(f"Project file: {info.project_path}")
    if info.recovery_path:
        lines.append(f"Recovery copy: {info.recovery_path}")
    fault = info.fault_text.strip()
    if fault:
        lines.append("")
        lines.append(fault)
    return "\n".join(lines) or "No extra detail was recorded."


class BugReportDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        session: Session,
        *,
        title: str = "",
        what: str = "",
        steps: str = "",
        contact: str = "",
        traceback: str = "",
        crash_id: str = "",
        include_project: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setProperty("_pc_own_fit", True)
        self.setObjectName("bugReportDialog")
        self.setWindowTitle("Report a bug")
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self._session = session
        self._window = parent
        self._traceback = traceback
        self._crash_id = crash_id
        self._created = _utc_now()

        compact = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("bugTitle")
        self.title_edit.setPlaceholderText("Short summary")
        self.title_edit.setText(title)
        self.what = QTextEdit()
        self.what.setPlaceholderText("What happened?")
        self.what.setPlainText(what)
        self.what.setFixedHeight(72)
        self.what.setSizePolicy(compact)
        self.steps = QTextEdit()
        self.steps.setPlaceholderText("What did you do just before this?")
        self.steps.setPlainText(steps)
        self.steps.setFixedHeight(84)
        self.steps.setSizePolicy(compact)
        self.contact = QLineEdit()
        self.contact.setPlaceholderText("Optional")
        self.contact.setText(contact)

        self.shot = QCheckBox("Screenshot of the main window")
        self.shot.setChecked(True)
        self.project_box = QCheckBox("Current project file")
        self.project_box.setChecked(include_project)
        self.log_box = QCheckBox("Last log")
        self.log_box.setChecked(True)
        self.sys_box = QCheckBox("System info")
        self.sys_box.setChecked(True)

        self.show_preview = QCheckBox("Show what will be sent")
        self.show_preview.setObjectName("showPreview")
        self.show_preview.setChecked(False)
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("bugPreview")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.preview.setVisible(False)
        self.preview.setMinimumHeight(120)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Report a bug"))
        form = QFormLayout()
        form.addRow("Title", self.title_edit)
        form.addRow("What happened", self.what)
        form.addRow("Steps to reproduce", self.steps)
        form.addRow("Contact name", self.contact)
        root.addLayout(form)

        include = QGroupBox("Include")
        grid = QGridLayout(include)
        boxes = (self.shot, self.project_box, self.log_box, self.sys_box)
        for index, box in enumerate(boxes):
            grid.addWidget(box, index // 2, index % 2)
        root.addWidget(include)
        root.addWidget(self.show_preview)
        root.addWidget(self.preview, 1)
        self.destination = QLabel(destination_line())
        self.destination.setObjectName("bugDestination")
        self.destination.setWordWrap(True)
        root.addWidget(self.destination)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(lambda: self._commit(send=False))
        self.save_btn = save
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("sendButton")
        self.send_btn.clicked.connect(lambda: self._commit(send=True))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self.title_edit.textChanged.connect(self.refresh_preview)
        self.what.textChanged.connect(self.refresh_preview)
        self.what.textChanged.connect(self._refresh_send)
        self.steps.textChanged.connect(self.refresh_preview)
        self.contact.textChanged.connect(self.refresh_preview)
        for box in boxes:
            box.toggled.connect(self.refresh_preview)
        self.show_preview.toggled.connect(self._toggle_preview)
        self.refresh_preview()
        self._refresh_send()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._refresh_send()
        self._fit()

    def _refresh_send(self) -> None:
        # Enabled with or without an endpoint, once the description is filled.
        # A click with nowhere to send still says the report was only saved.
        self.destination.setText(destination_line())
        ready = bool(self.what.toPlainText().strip())
        self.send_btn.setEnabled(ready)
        self.save_btn.setEnabled(ready)
        tip = "" if ready else "Describe what happened."
        self.send_btn.setToolTip(tip)
        self.save_btn.setToolTip(tip)

    def _toggle_preview(self, open_: bool) -> None:
        if open_:
            self.preview.setVisible(True)
            self._fit()
            return
        self.preview.setMinimumHeight(0)
        self.preview.setMaximumHeight(16777215)
        self.preview.setVisible(False)
        self.setMinimumSize(0, 0)
        QTimer.singleShot(0, self, self._collapse_to_contents)

    def _collapse_to_contents(self) -> None:
        """Shrink after the layout has dropped the preview. A direct resize stays tall."""

        self.setMinimumSize(0, 0)
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.adjustSize()
        self._fit()

    def _fit(self) -> None:
        _sw, sh = screen_limit(self)
        max_h = max(280, sh - 48)
        if self.preview.isVisible():
            budget = max(140, max_h - 470)
            self.preview.setMinimumHeight(budget)
            self.preview.setMaximumHeight(budget)
        clamp_dialog(self, width=760)

    def _title(self) -> str:
        typed = self.title_edit.text().strip()
        if typed:
            return typed.splitlines()[0].strip()[:120]
        text = self.what.toPlainText().strip()
        if not text:
            return "PanelCAD bug report"
        return text.splitlines()[0].strip()[:120]

    def _preview_request(self) -> ReportRequest:
        return ReportRequest(
            title=self._title(),
            description=self.what.toPlainText().strip(),
            steps=self.steps.toPlainText().strip(),
            contact=self.contact.text().strip(),
            include_log=self.log_box.isChecked(),
            include_system=self.sys_box.isChecked(),
            include_screenshot=self.shot.isChecked(),
            include_project=self.project_box.isChecked(),
            screenshot_png=b"png" if self.shot.isChecked() else None,
            project_bytes=b"project" if self.project_box.isChecked() else None,
            project_source=str(self._session.path or ""),
            traceback=self._traceback,
            created=self._created,
            crash_id=self._crash_id,
            recent_actions=recent_actions_text(self._session),
        )

    def refresh_preview(self) -> None:
        text = preview_text(self._preview_request())
        if len(text) > 20_000:
            text = text[:20_000] + "\n… preview truncated; Save writes the full log\n"
        self.preview.setPlainText(text)

    def _commit(self, *, send: bool) -> None:
        if not self.what.toPlainText().strip():
            QMessageBox.information(self, "Report a bug", "Describe what happened.")
            return
        png = grab_png(self._window) if self.shot.isChecked() else None
        try:
            result = self._session.report_bug(
                self._title(),
                self.what.toPlainText().strip(),
                include_project=self.project_box.isChecked(),
                include_screenshot=bool(png),
                send=send,
                steps=self.steps.toPlainText().strip(),
                contact=self.contact.text().strip(),
                screenshot_png=png,
                traceback=self._traceback,
                crash_id=self._crash_id,
                created=self._created,
                include_log=self.log_box.isChecked(),
                include_system=self.sys_box.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Report a bug", str(exc))
            return
        path = str(result.get("path") or "")
        if not send:
            QMessageBox.information(self, "Report a bug", f"Saved on this computer:\n{path}")
            self.accept()
            return
        ok, text = _send_notice(result)
        if ok:
            QMessageBox.information(self, "Report a bug", text)
            self.accept()
            return
        QMessageBox.warning(self, "Report a bug", text)


class CrashDialog(QDialog):
    def __init__(self, parent: QWidget | None, record: dict) -> None:
        super().__init__(parent)
        self.setProperty("_pc_own_fit", True)
        self.setObjectName("crashDialog")
        self.setWindowTitle("PanelCAD hit an error")
        self._record = dict(record)
        self._path = str(record.get("path") or "")
        detail = self._load()
        self._session = _session_or_new(parent)

        root = QVBoxLayout(self)
        title = QLabel("PanelCAD hit an error")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)
        root.addWidget(
            _wrapping(
                "A crash record is on this computer. "
                "Report fills in a bug report. Save report writes a zip. Close keeps working."
            )
        )
        recovery = str(detail.get("recovery_path") or self._record.get("recovery_path") or "")
        project_path = str(detail.get("project_path") or self._record.get("project_path") or "")
        project_name = str(detail.get("project_name") or self._record.get("project_name") or "")
        sentence = recovery_sentence(
            project_path=project_path,
            project_name=project_name,
            recovery_path=recovery,
            recovery_exists=bool(recovery) and Path(recovery).is_file(),
        )
        recovery_label = _wrapping(sentence)
        recovery_label.setObjectName("crashRecovery")
        root.addWidget(recovery_label)
        tb = str(detail.get("traceback") or "")
        frame, exc_line = traceback_summary(tb)
        if not exc_line:
            exc_line = str(detail.get("exception") or "").strip()
        summary = "\n".join(line for line in (frame, exc_line) if line)
        if summary:
            first_label = _wrapping(summary)
            first_label.setObjectName("crashTraceFirst")
            root.addWidget(first_label)
        self.tb_toggle = QCheckBox("Show traceback")
        self.tb_toggle.setObjectName("showTraceback")
        self.tb_toggle.setChecked(False)
        self.tb_view = QPlainTextEdit()
        self.tb_view.setObjectName("crashTrace")
        self.tb_view.setReadOnly(True)
        self.tb_view.setPlainText(tb)
        self.tb_view.setVisible(False)
        self.tb_view.setMinimumHeight(80)
        self.tb_view.setMaximumHeight(160)
        if tb.strip():
            root.addWidget(self.tb_toggle)
            root.addWidget(self.tb_view, 1)
            self.tb_toggle.toggled.connect(self._toggle_trace)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        report = QPushButton("Report")
        report.setObjectName("primary")
        report.clicked.connect(self._report)
        save = QPushButton("Save report")
        save.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(report)
        buttons.addWidget(save)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._fit()

    def _toggle_trace(self, open_: bool) -> None:
        self.tb_view.setVisible(open_)
        self._fit()

    def _fit(self) -> None:
        clamp_dialog(self, width=680)

    def _load(self) -> dict:
        if self._path and Path(self._path).is_file():
            try:
                return load_crash(self._path)
            except (OSError, ValueError):
                return dict(self._record)
        return dict(self._record)

    def _save(self) -> None:
        detail = self._load()
        try:
            result = self._session.report_bug(
                "PanelCAD hit an error",
                str(detail.get("exception") or "Unhandled error"),
                include_project=False,
                include_screenshot=False,
                send=False,
                traceback=str(detail.get("traceback") or ""),
                crash_id=str(detail.get("id") or self._record.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "PanelCAD hit an error", str(exc))
            return
        QMessageBox.information(
            self,
            "PanelCAD hit an error",
            f"Saved on this computer:\n{result.get('path')}",
        )

    def _report(self) -> None:
        detail = self._load()
        dlg = BugReportDialog(
            self.parentWidget() or self,
            self._session,
            title="PanelCAD hit an error",
            what=str(detail.get("exception") or "Unhandled error"),
            traceback=str(detail.get("traceback") or ""),
            crash_id=str(detail.get("id") or ""),
        )
        dlg.exec()


class RecoveryDialog(QDialog):
    def __init__(self, parent: QWidget | None, session: Session, info: UncleanExit) -> None:
        super().__init__(parent)
        self.setProperty("_pc_own_fit", True)
        self.setObjectName("recoveryDialog")
        self.setWindowTitle("PanelCAD")
        self._session = session
        self._info = info
        self._window = parent
        recovery = info.recovery_path
        has_recovery = bool(recovery) and Path(recovery).is_file()

        root = QVBoxLayout(self)
        title = QLabel("PanelCAD closed unexpectedly last time.")
        title.setObjectName("recoveryLead")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        title.setWordWrap(True)
        root.addWidget(title)
        when = plain_when(info.when)
        if when:
            root.addWidget(_wrapping(f"Last active {when}"))
        root.addWidget(_heading("Project", "recoveryProjectHeading"))
        if info.project_name:
            root.addWidget(_wrapping(info.project_name))
        if info.project_path:
            root.addWidget(_ElidedPath(info.project_path, object_name="recoveryProjectPath"))
        if not info.project_name and not info.project_path:
            root.addWidget(QLabel("No project was open."))
        root.addWidget(_heading("Recovery copy", "recoveryCopyHeading"))
        if has_recovery:
            copy_row = QHBoxLayout()
            copy_row.addWidget(_ElidedPath(recovery, object_name="recoveryCopyPath"), 1)
            opener = QPushButton("Open folder")
            opener.setObjectName("recoveryOpenFolder")
            opener.clicked.connect(lambda _checked=False, path=recovery: _open_folder(path))
            copy_row.addWidget(opener)
            root.addLayout(copy_row)
        else:
            root.addWidget(QLabel("No recovery copy was saved."))
        self.details = QCheckBox("Details")
        self.details.setObjectName("recoveryDetails")
        self.details.setChecked(False)
        self.details_view = QPlainTextEdit()
        self.details_view.setObjectName("recoveryDetailsText")
        self.details_view.setReadOnly(True)
        self.details_view.setPlainText(_recovery_details(info))
        self.details_view.setVisible(False)
        self.details_view.setMinimumHeight(72)
        self.details_view.setMaximumHeight(140)
        self.details.toggled.connect(self._toggle_details)
        root.addWidget(self.details)
        root.addWidget(self.details_view)
        self.destination = QLabel(destination_line())
        self.destination.setObjectName("recoveryDestination")
        self.destination.setWordWrap(True)
        root.addWidget(self.destination)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        send = QPushButton("Send report")
        send.setObjectName("primary")
        send.clicked.connect(self._send)
        send.setEnabled(True)
        reopen = QPushButton("Reopen recovery copy")
        reopen.setEnabled(has_recovery)
        reopen.clicked.connect(self._reopen)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(send)
        buttons.addWidget(reopen)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._fit()

    def _toggle_details(self, open_: bool) -> None:
        self.details_view.setVisible(open_)
        self._fit()

    def _fit(self) -> None:
        clamp_dialog(self, width=640)

    def _send(self) -> None:
        description = "PanelCAD closed unexpectedly last time."
        if self._info.project_name or self._info.project_path:
            description += f"\nProject: {self._info.project_name} {self._info.project_path}".rstrip()
        if self._info.recovery_path:
            description += f"\nRecovery copy: {self._info.recovery_path}"
        traceback = self._info.fault_text
        crash_id = self._info.crash_id
        if self._info.crash_path and Path(self._info.crash_path).is_file():
            try:
                detail = load_crash(self._info.crash_path)
            except (OSError, ValueError):
                detail = {}
            traceback = str(detail.get("traceback") or traceback)
            crash_id = str(detail.get("id") or crash_id)
            if detail.get("exception"):
                description = str(detail["exception"]) + "\n" + description
        try:
            result = self._session.report_bug(
                "PanelCAD closed unexpectedly",
                description,
                include_project=False,
                include_screenshot=False,
                send=True,
                traceback=traceback,
                crash_id=crash_id,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "PanelCAD", str(exc))
            return
        ok, text = _send_notice(result)
        if ok:
            QMessageBox.information(self, "PanelCAD", text)
        else:
            QMessageBox.warning(self, "PanelCAD", text)

    def _reopen(self) -> None:
        path = Path(self._info.recovery_path)
        window = self._window
        if window is None or not hasattr(window, "open_recovery"):
            QMessageBox.warning(self, "PanelCAD", f"Recovery copy:\n{path}")
            return
        project = self._info.project_path.strip() or None
        window.open_recovery(path, project)
        self.accept()




def __getattr__(name: str):
    """Settings lives with Appearance. Importing it from here stays valid."""

    if name == "SettingsDialog":
        from panelcad.gui.settings_dialog import SettingsDialog

        return SettingsDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



def _session_or_new(parent: QWidget | None) -> Session:
    session = getattr(parent, "session", None)
    if isinstance(session, Session):
        return session
    return Session.create("Crash report")


def offer_unclean_exit(window: QWidget, info: UncleanExit) -> None:
    from panelcad.gui.splash import close_splash

    close_splash()
    session = getattr(window, "session", None)
    if not isinstance(session, Session):
        session = Session.create("Untitled Panel")
    try:
        RecoveryDialog(window, session, info).exec()
    finally:
        acknowledge_unclean(info)
