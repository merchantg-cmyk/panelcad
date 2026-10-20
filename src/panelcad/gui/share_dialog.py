"""Dialogs for a project that lives in a synced folder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from panelcad.io.share import LockInfo, last_saved_text


def ask_locked(parent, holder: LockInfo | None) -> str:
    """``readonly``, ``takeover``, or ``cancel``."""

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Project locked")
    if holder is not None and not holder.unreadable:
        who = holder.user or "Someone"
        host = holder.host or "another PC"
        box.setText(f"{who} on {host} has this project open.")
        when = holder.heartbeat or holder.acquired
        clock = f"Lock time: {when}.\n\n" if when else ""
        box.setInformativeText(
            clock
            + "Open read-only, or take over the lock.\n"
            "Taking over does not bring in their unsaved work. "
            "If they save later, PanelCAD will not overwrite the file silently."
        )
    else:
        box.setText("This project lock could not be read.")
        box.setInformativeText(
            "Open read-only, or take over the lock. Taking over replaces the lock file."
        )
    read_only = box.addButton("Read-only", QMessageBox.ButtonRole.AcceptRole)
    take_over = box.addButton("Take over", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(read_only)
    box.exec()
    clicked = box.clickedButton()
    if clicked is cancel or clicked is None:
        return "cancel"
    if clicked is not take_over:
        return "readonly"
    who = holder.label() if holder is not None else "the other person"
    confirm = QMessageBox.warning(
        parent,
        "Take over the lock",
        f"Take over the lock from {who}?\n\n"
        "They can keep the file open. Their next save will not overwrite this one silently.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if confirm != QMessageBox.StandardButton.Yes:
        return "cancel"
    return "takeover"


def ask_conflict(parent, path: Path, copies: list[Path] | None = None) -> str:
    """``reload``, ``copy``, ``review``, or ``cancel``."""

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Project changed on disk")
    box.setText(f"{Path(path).name} changed on disk since it was opened.")
    extra = "It was not overwritten."
    names = [item.name for item in (copies or [])]
    if names:
        extra += "\n\nSync conflict copies are next to it: " + ", ".join(names[:6]) + "."
    extra += "\n\nReload the disk copy, save your work as a new file, or review the differences."
    box.setInformativeText(extra)
    reload_btn = box.addButton("Reload", QMessageBox.ButtonRole.AcceptRole)
    copy_btn = box.addButton("Save a copy", QMessageBox.ButtonRole.ActionRole)
    review_btn = box.addButton("Review differences", QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is reload_btn:
        return "reload"
    if clicked is copy_btn:
        return "copy"
    if clicked is review_btn:
        return "review"
    return "cancel"


class DiffDialog(QDialog):
    """Text diff. The choice is ``reload``, ``copy``, or ``cancel``."""

    def __init__(self, parent, diff: str) -> None:
        super().__init__(parent)
        self.choice = "cancel"
        self.setWindowTitle("Review differences")
        self.resize(760, 480)
        note = QLabel("On disk compared with this session. Nothing has been written yet.")
        note.setWordWrap(True)
        self.text = QPlainTextEdit(diff)
        self.text.setReadOnly(True)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(font)
        reload_btn = QPushButton("Reload")
        copy_btn = QPushButton("Save a copy")
        close_btn = QPushButton("Close")
        reload_btn.clicked.connect(lambda: self._pick("reload"))
        copy_btn.clicked.connect(lambda: self._pick("copy"))
        close_btn.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(reload_btn)
        row.addWidget(copy_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.text, 1)
        layout.addLayout(row)

    def _pick(self, choice: str) -> None:
        self.choice = choice
        self.accept()


def ask_diff(parent, diff: str) -> str:
    dialog = DiffDialog(parent, diff)
    dialog.exec()
    return dialog.choice


def confirm_discard(parent) -> bool:
    answer = QMessageBox.question(
        parent,
        "Refresh from disk",
        "Discard unsaved changes and reload the project file?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def warn_conflict_copies(parent, copies: list[Path]) -> None:
    names = "\n".join(item.name for item in copies[:12])
    QMessageBox.warning(
        parent,
        "Sync conflict copies",
        "PanelCAD found sync conflict copies next to this project.\n"
        "The project file was not changed.\n\n"
        f"{names}",
    )


class ProjectActivityDialog(QDialog):
    """Who saved this project, newest first."""

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project activity")
        self.resize(640, 420)
        headline = last_saved_text(session.project) or "No one has saved this project yet."
        banner = QLabel(headline)
        banner.setWordWrap(True)
        rows = list(getattr(session.project, "activity", []) or [])
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["User", "PC", "Time", "Summary"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for event in reversed(rows):
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                str(getattr(event, "user", "") or ""),
                str(getattr(event, "host", "") or ""),
                str(getattr(event, "time", "") or ""),
                str(getattr(event, "summary", "") or ""),
            )
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(banner)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)
