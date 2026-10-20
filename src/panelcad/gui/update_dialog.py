"""Help → Updates, the update prompt, and the post-update notice."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from panelcad.update.github_releases import PUBLIC_GITHUB_REPOSITORY
from panelcad.update.settings import UpdateSettings, builtin_feed_active, github_releases_active

DIALOG_STYLE = """
QDialog { background: #f7f4ea; color: #1a1a1a; }
QLabel { color: #1a1a1a; }
QPlainTextEdit, QLineEdit {
    background: #ffffff;
    color: #1a1a1a;
    border: 1px solid #c8c2b4;
    padding: 4px;
}
QCheckBox { color: #1a1a1a; }
QPushButton {
    background: #f7f4ea;
    color: #1a1a1a;
    border: 1px solid #1e3a5f;
    padding: 6px 14px;
}
QPushButton#updateInstall, QPushButton#updateRollback {
    background: #1e3a5f;
    color: #f7f4ea;
}
"""


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont()
    font.setPointSize(14)
    font.setBold(True)
    label.setFont(font)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ElidedPathLabel(QLabel):
    """One line, middle-elided, selectable. A long backup path must not wrap mid-word."""

    def __init__(self, full: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = full
        self.setObjectName("updateBackupPath")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWordWrap(False)
        self.setToolTip(full)
        self.setText(full)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = max(40, self.contentsRect().width())
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, width))


class UpdateProgressDialog(QDialog):
    """Modal progress for a feed check or an install. Cancel stops the work."""

    def __init__(
        self,
        title: str,
        detail: str,
        parent: QWidget | None = None,
        *,
        on_cancel=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(480)
        self.cancelled = False
        self._on_cancel = on_cancel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.label = QLabel(detail)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("updateCancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def set_detail(self, text: str) -> None:
        self.label.setText(text)

    def reject(self) -> None:
        self.cancelled = True
        if self._on_cancel is not None:
            self._on_cancel()
        super().reject()


class UpdateSettingsDialog(QDialog):
    """Where the feed lives, and whether startup checks are on."""

    def __init__(self, settings: UpdateSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Updates")
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(720)
        self.check_now = False
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Updates"))
        note = QLabel(
            "PanelCAD checks a release feed you choose. "
            "PANELCAD_UPDATE_FEED wins, then this field, then a feed built into a shop release. "
            "A public build with none of those offers download from the public GitHub Releases. "
            "Nothing is installed until you click Install now. "
            "A synced folder works as a file path. "
            "An https Google Drive direct link (uc?export=download&id=…) also works."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.feed = QLineEdit()
        self.feed.setPlaceholderText(r"https://…/panelcad-feed.json  or  D:\PanelCAD-feed")
        self.feed.setText(settings.feed)
        self.feed.setClearButtonEnabled(True)
        feed_row = QHBoxLayout()
        feed_row.addWidget(self.feed, 1)
        file_btn = QPushButton("File…")
        file_btn.clicked.connect(self._browse_file)
        folder_btn = QPushButton("Folder…")
        folder_btn.clicked.connect(self._browse_folder)
        feed_row.addWidget(file_btn)
        feed_row.addWidget(folder_btn)
        form.addRow("Feed", feed_row)

        self.startup = QCheckBox("Check for updates at startup (at most once a day)")
        self.startup.setChecked(settings.check_on_startup)
        form.addRow("", self.startup)
        self.keep = QSpinBox()
        self.keep.setRange(1, 50)
        self.keep.setValue(max(1, int(settings.keep_backups or 3)))
        self.keep.setMaximumWidth(80)
        form.addRow("Backups to keep", self.keep)
        layout.addLayout(form)

        skip = settings.skip_version or "none"
        self.skip_label = QLabel(f"Skip this version: {skip}")
        self.skip_label.setWordWrap(True)
        self.skip_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.skip_label)
        self._clear_skip = False
        if settings.skip_version:
            clear = QPushButton("Clear skipped version")
            clear.clicked.connect(self._on_clear_skip)
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(clear)
            layout.addLayout(row)

        self.builtin = QLabel("Using the built-in PanelCAD update feed")
        self.builtin.setObjectName("builtinUpdateFeed")
        self.builtin.setWordWrap(True)
        self.builtin.setVisible(builtin_feed_active(settings))
        layout.addWidget(self.builtin)
        github_on = github_releases_active(settings)
        self.github = QLabel("Download update from the public GitHub Releases")
        self.github.setObjectName("githubReleasesUpdate")
        self.github.setWordWrap(True)
        self.github.setVisible(github_on)
        layout.addWidget(self.github)
        if github_on:
            placeholder = QLabel(
                f"Releases: https://github.com/{PUBLIC_GITHUB_REPOSITORY}/releases. "
                "PANELCAD_GITHUB_REPOSITORY=owner/name overrides that repository."
            )
            placeholder.setObjectName("githubReleasesPlaceholder")
            placeholder.setWordWrap(True)
            placeholder.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(placeholder)

        env = QLabel(
            "PANELCAD_UPDATE_FEED overrides this field and the built-in feed "
            "when that variable is set."
        )
        env.setWordWrap(True)
        env.setStyleSheet("color: #444;")
        layout.addWidget(env)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        check = QPushButton("Check now")
        check.clicked.connect(self._on_check_now)
        save = QPushButton("Save")
        save.setObjectName("updateInstall")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(check)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Release feed",
            self.feed.text(),
            "Feed JSON (*.json);;All files (*)",
        )
        if path:
            self.feed.setText(path)

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Folder that contains panelcad-feed.json",
            self.feed.text(),
        )
        if path:
            self.feed.setText(path)

    def _on_clear_skip(self) -> None:
        self._clear_skip = True
        self.skip_label.setText("Skip this version: none")

    def _on_check_now(self) -> None:
        self.check_now = True
        self.accept()

    def values(self) -> UpdateSettings:
        return UpdateSettings(
            feed=self.feed.text().strip(),
            check_on_startup=self.startup.isChecked(),
            keep_backups=int(self.keep.value()),
        )


class UpdateAvailableDialog(QDialog):
    """Version and notes. Install now, Later, or Skip this version. Never installs on open."""

    def __init__(self, report: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PanelCAD update")
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(640)
        self.choice = "later"
        version = str(report.get("version") or "")
        current = str(report.get("current") or "")
        when = str(report.get("release_date") or "")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(_heading(f"PanelCAD {version} is available"))
        if when:
            released = QLabel(when)
            released.setObjectName("updateReleaseDate")
            released.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(released)
        sub = QLabel(
            f"This copy is {current}. The download is checked against the published SHA-256 "
            "before anything runs."
        )
        sub.setWordWrap(True)
        sub.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(sub)
        meta_lines: list[str] = []
        size = report.get("size")
        if size not in (None, ""):
            meta_lines.append(f"Download size: {int(size)} bytes")
        feed = str(report.get("feed") or "").strip()
        if feed:
            meta_lines.append(f"Feed: {feed}")
        if meta_lines:
            meta = QLabel("\n".join(meta_lines))
            meta.setObjectName("updateMeta")
            meta.setWordWrap(True)
            meta.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(meta)
        if report.get("blocked"):
            blocked = QLabel(str(report.get("block_reason") or ""))
            blocked.setWordWrap(True)
            blocked.setTextFormat(Qt.TextFormat.PlainText)
            blocked.setStyleSheet("color: #8a1c13;")
            layout.addWidget(blocked)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlainText(str(report.get("notes") or "No release notes."))
        self.notes.document().setDocumentMargin(4)
        self.notes.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.notes)
        self._fit_notes(600)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later = QPushButton("Later")
        later.setObjectName("updateLater")
        later.setDefault(True)
        later.setAutoDefault(True)
        later.clicked.connect(self._later)
        skip = QPushButton("Skip this version")
        skip.setObjectName("updateSkip")
        skip.clicked.connect(self._skip)
        install = QPushButton("Install now")
        install.setObjectName("updateInstall")
        install.setAutoDefault(False)
        install.setDefault(False)
        install.setEnabled(bool(report.get("installable")))
        install.clicked.connect(self._install)
        buttons.addWidget(install)
        buttons.addWidget(later)
        buttons.addWidget(skip)
        layout.addLayout(buttons)

    def _fit_notes(self, width: int) -> None:
        """Size the notes box from font metrics. QTextDocument.size() is unreliable offscreen."""

        text_width = max(280, width)
        metrics = self.notes.fontMetrics()
        lines = 0
        for line in self.notes.toPlainText().splitlines() or [""]:
            advance = max(1, metrics.horizontalAdvance(line))
            lines += max(1, (advance + text_width - 1) // text_width)
        frame = self.notes.frameWidth() * 2 + 16
        height = lines * metrics.lineSpacing() + frame
        self.notes.setFixedHeight(min(240, max(72, int(height))))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_notes(max(280, self.notes.viewport().width()))
        self.adjustSize()

    def _install(self) -> None:
        self.choice = "install"
        self.accept()

    def _later(self) -> None:
        self.choice = "later"
        self.reject()

    def _skip(self) -> None:
        self.choice = "skip"
        self.accept()


class UpdateNoticeDialog(QDialog):
    """Shown once after an upgrade or a rollback relaunches PanelCAD."""

    def __init__(
        self,
        version: str,
        *,
        kind: str = "update",
        message: str = "",
        log: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("PanelCAD")
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        if kind == "failed":
            headline = "rollback/update failed: see log"
            detail = message or (
                "The last install or roll back did not finish. Drawings were not moved."
            )
        elif kind == "rollback":
            headline = f"Rolled back to {version}"
            detail = (
                "Program files were restored from the saved copy. "
                "Drawings, settings, logs, and the user part library were left in place."
            )
        else:
            headline = f"Updated to {version}"
            detail = (
                "PanelCAD updated. Drawings, settings, logs, and the user part library were kept. "
                "Help > Roll back to previous version restores the saved copy."
            )
        layout.addWidget(_heading(headline))
        body = QLabel(detail)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(body)
        if kind == "failed" and log:
            layout.addWidget(ElidedPathLabel(log))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton("OK")
        ok.setObjectName("updateInstall")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)


class RollbackDialog(QDialog):
    def __init__(self, previous_version: str, backup: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Roll back")
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(640)
        self.confirmed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(_heading(f"Roll back to {previous_version}?"))
        body = QLabel(
            "This replaces src, scripts, and packaging files such as pyproject.toml "
            "from the saved copy, then reinstalls them into the venv. Drawings and "
            "other files in the program folder stay where they are."
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(body)
        layout.addWidget(ElidedPathLabel(backup))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        go = QPushButton("Roll back")
        go.setObjectName("updateRollback")
        go.setAutoDefault(False)
        go.clicked.connect(self._confirm)
        buttons.addWidget(go)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _confirm(self) -> None:
        self.confirmed = True
        self.accept()


def settings_from_dialog(dialog: UpdateSettingsDialog, previous: UpdateSettings) -> UpdateSettings:
    """Keep last_check, and clear skip only when the user asked."""

    values = dialog.values()
    values.last_check = previous.last_check
    values.skip_version = "" if dialog._clear_skip else previous.skip_version
    return values
