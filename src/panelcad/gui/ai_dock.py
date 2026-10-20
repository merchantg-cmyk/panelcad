"""Human-visible AI status: who is driving Session, which page, and the op log."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.activity import DEFAULT_MODEL


class AiDock(QWidget):
    """Shows the AI (never the mouse) working: model, page, live operations."""

    prompt_submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model_name = ""
        self.status_name = "idle"
        self.setMinimumWidth(180)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        from panelcad.gui.theme import banner_stylesheet
        from panelcad.theme import MARGIN_PX, SPACING_PX

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)

        self.banner = QLabel("No AI connected")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(banner_stylesheet("neutral"))
        layout.addWidget(self.banner)

        self.meta = QLabel("Model  —\nPage  —")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)

        self.log = QListWidget()
        self.log.setAlternatingRowColors(True)
        self.log.setWordWrap(True)
        self.log.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log.setSpacing(2)
        layout.addWidget(self.log, 1)

        self.prompt = QLineEdit()
        self.prompt.setPlaceholderText("Tell Grok what to do on this drawing…")
        self.prompt.setClearButtonEnabled(True)
        self.prompt.returnPressed.connect(self._send_prompt)
        send = QPushButton("Send")
        send.clicked.connect(self._send_prompt)
        prompt_row = QHBoxLayout()
        prompt_row.addWidget(self.prompt, 1)
        prompt_row.addWidget(send)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)
        layout.addLayout(prompt_row)

    def _banner_status(self) -> str:
        if self.status_name == "working":
            return "working"
        if self.status_name == "error":
            return "error"
        if self.status_name == "idle" and self.model_name:
            return "idle"
        if self.status_name == "warning":
            return "warning"
        return "neutral"

    def apply_theme(self) -> None:
        from panelcad.gui.theme import banner_stylesheet

        self.banner.setStyleSheet(banner_stylesheet(self._banner_status()))
        for index in range(self.log.count()):
            item = self.log.item(index)
            event = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(event, dict):
                color = self._log_color(event)
                if color is not None:
                    item.setForeground(color)

    def _log_color(self, event: dict):
        from panelcad.gui.theme import ui_qcolor

        actor = event.get("actor") or {}
        kind = str(actor.get("kind") or "")
        op = str(event.get("op") or "")
        if event.get("error"):
            return ui_qcolor("log_error")
        if op == "prompt" or kind == "human":
            return ui_qcolor("log_human")
        if kind == "ai":
            return ui_qcolor("log_ai")
        return None

    def _refresh_meta(self, page: str | None = None) -> None:
        model = self.model_name or "—"
        page_txt = page or self.meta.text().split("\n", 1)[-1].replace("Page  ", "", 1)
        if page_txt.startswith("Model"):
            page_txt = "—"
        self.meta.setText(f"Model  {model}\nPage  {page_txt}")

    def set_page(
        self,
        index: int,
        count: int,
        title: str,
        kind: str,
        sheet_id: str,
        project_name: str = "",
    ) -> None:
        label = title or sheet_id
        if project_name and project_name != label:
            label = f"{project_name} — {label}"
        page = f"{index}/{count}  {label}  ·  {kind}"
        self._refresh_meta(page)
        if self.status_name == "working" and self.model_name:
            self.banner.setText(f"{self.model_name} is working on {label}")

    def apply_event(self, event: dict) -> None:
        actor = event.get("actor") or {}
        kind = str(actor.get("kind") or "")
        model = str(actor.get("model") or "")
        status = str(event.get("status") or "")
        if model:
            self.model_name = model
            self.prompt.setPlaceholderText(f"Tell {model} what to do on this drawing…")
        if status in {"working", "idle", "error"}:
            self.status_name = status
        title = str(event.get("sheet_title") or event.get("sheet") or "")

        if self.status_name == "working":
            who = self.model_name or DEFAULT_MODEL
            page = title or "the drawing"
            self.banner.setText(f"{who} is working on {page}")
        elif self.status_name == "error":
            self.banner.setText(str(event.get("error") or event.get("summary") or "AI error"))
        elif self.status_name == "idle" and self.model_name:
            self.banner.setText(f"{self.model_name} idle")
        elif kind == "ai":
            self.banner.setText("No AI connected")
        from panelcad.gui.theme import banner_stylesheet

        self.banner.setStyleSheet(banner_stylesheet(self._banner_status()))

        stamp = str(event.get("t") or "")[-8:]
        op = str(event.get("op") or "")
        summary = str(event.get("summary") or op)
        err = event.get("error")
        text = f"{stamp}  {summary}"
        if err and str(err) not in text:
            text += f"  — {err}"
        item = QListWidgetItem(text)
        item.setToolTip(text)
        color = self._log_color(event)
        if color is not None:
            item.setForeground(color)
        item.setData(Qt.ItemDataRole.UserRole, event)
        self.log.addItem(item)
        self.log.scrollToBottom()
        while self.log.count() > 200:
            self.log.takeItem(0)

    def load_events(self, events: list[dict]) -> None:
        self.log.clear()
        for event in events[-80:]:
            self.apply_event(event)

    def _send_prompt(self) -> None:
        text = self.prompt.text().strip()
        if not text:
            return
        self.prompt.clear()
        self.prompt_submitted.emit(text)
