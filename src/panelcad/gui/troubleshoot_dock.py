"""Troubleshoot dock: a guided walk for a person, using the drawing's own data.

The same guide payload is what an AI reads. Explain, when it is used, calls
only the account in Settings > AI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from panelcad.core.troubleshoot import SYMPTOMS
from panelcad.core.troubleshoot_guide import (
    HOW,
    device_choices,
    guide,
    write_session_csv,
)
from panelcad.core.xref_index import QueryError
from panelcad.gui.theme import ui_font


class _ExplainWorker(QThread):
    """Runs Explain off the UI thread. The dock paints the result."""

    finished_result = Signal(int, dict)

    def __init__(
        self,
        generation: int,
        project: Any,
        catalog: Any,
        payload: dict[str, Any],
        transport: Any,
        cancel: Any,
    ) -> None:
        super().__init__()
        self._generation = generation
        self._project = project
        self._catalog = catalog
        self._payload = payload
        self._transport = transport
        self._cancel = cancel

    def run(self) -> None:
        from panelcad.ai.troubleshoot_explain import explain_guide

        if self._cancel.cancelled():
            result: dict[str, Any] = {"ok": False, "cancelled": True, "text": "", "reason": ""}
        else:
            result = explain_guide(
                self._project,
                self._catalog,
                self._payload,
                transport=self._transport,
                cancel=self._cancel,
            )
        self.finished_result.emit(self._generation, result)


class TroubleshootDock(QWidget):
    """Symptom, device, checks, and a log. Big result buttons, one next step."""

    highlight_requested = Signal(dict)
    status_message = Signal(str)
    settings_requested = Signal()
    help_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("troubleshootPanel")
        self._session = None
        self._guide: dict[str, Any] | None = None
        self._marks: dict[str, str] = {}
        self._cards: QVBoxLayout | None = None
        self._explain_worker: _ExplainWorker | None = None
        self._explain_call: Any = None
        self._explain_generation = 0
        self._explain_running = False

        from panelcad.theme import MARGIN_PX, SPACING_PX

        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        root.setSpacing(SPACING_PX)

        self.banner = QLabel("")
        self.banner.setObjectName("troubleshootBanner")
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.banner)
        self._set_banner()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(SPACING_PX)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        from panelcad.gui.guidance import EmptyState

        self.empty = EmptyState()
        self.empty.clicked.connect(self._empty_clicked)
        self._body.addWidget(self.empty)

        self.intro = QLabel(HOW)
        self.intro.setObjectName("troubleshootHow")
        self.intro.setWordWrap(True)
        self._body.addWidget(self.intro)

        pick = QHBoxLayout()
        self.symptom = QComboBox()
        self.symptom.setObjectName("troubleshootSymptom")
        self.symptom.setFont(ui_font())
        for item in SYMPTOMS.values():
            self.symptom.addItem(item["title"], item["id"])
        self.symptom.setToolTip("What is wrong. The checks follow that symptom.")
        self.device = QComboBox()
        self.device.setObjectName("troubleshootDevice")
        self.device.setFont(ui_font())
        self.device.setToolTip("Device tag from the cross-reference index.")
        self.start_button = QPushButton("Show the checks")
        self.start_button.setObjectName("troubleshootStart")
        self.start_button.setMinimumHeight(36)
        self.start_button.clicked.connect(self.start)
        pick.addWidget(self.symptom, 1)
        pick.addWidget(self.device, 1)
        pick.addWidget(self.start_button)
        self._body.addLayout(pick)
        self.symptom.currentIndexChanged.connect(self._on_picker_changed)
        self.device.currentIndexChanged.connect(self._on_picker_changed)

        self.prompt = QLabel("Pick a symptom and a device, then show the checks.")
        self.prompt.setObjectName("troubleshootPrompt")
        self.prompt.setWordWrap(True)
        font = ui_font()
        font.setBold(True)
        self.prompt.setFont(font)
        self._body.addWidget(self.prompt)

        self.progress = QLabel("")
        self.progress.setObjectName("troubleshootProgress")
        self.progress.setWordWrap(True)
        self._body.addWidget(self.progress)

        self.path_label = QLabel("")
        self.path_label.setObjectName("troubleshootPath")
        self.path_label.setWordWrap(True)
        self._body.addWidget(self.path_label)

        self.xref_label = QLabel("")
        self.xref_label.setObjectName("troubleshootXref")
        self.xref_label.setWordWrap(True)
        self._body.addWidget(self.xref_label)

        self.fault = QLabel("")
        self.fault.setObjectName("troubleshootFault")
        self.fault.setWordWrap(True)
        self._body.addWidget(self.fault)

        self.causes = QLabel("")
        self.causes.setObjectName("troubleshootCauses")
        self.causes.setWordWrap(True)
        self._body.addWidget(self.causes)

        show = QPushButton("Show on the sheet")
        show.setObjectName("troubleshootShow")
        show.setMinimumHeight(36)
        show.clicked.connect(self._emit_highlight)
        self._body.addWidget(show)

        self._cards = QVBoxLayout()
        self._body.addLayout(self._cards)

        log_row = QHBoxLayout()
        self.who = QLineEdit()
        self.who.setObjectName("troubleshootWho")
        self.who.setPlaceholderText("Your name")
        self.note = QLineEdit()
        self.note.setObjectName("troubleshootNote")
        self.note.setPlaceholderText("Note for the job file")
        log_row.addWidget(self.who, 1)
        log_row.addWidget(self.note, 2)
        self._body.addLayout(log_row)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Save log")
        self.save_button.setObjectName("troubleshootSave")
        self.save_button.setMinimumHeight(36)
        self.save_button.clicked.connect(self.save_log)
        self.csv_button = QPushButton("Export CSV…")
        self.csv_button.setObjectName("troubleshootCsv")
        self.csv_button.setMinimumHeight(36)
        self.csv_button.clicked.connect(self._export_csv_dialog)
        self.pdf_button = QPushButton("Export PDF…")
        self.pdf_button.setObjectName("troubleshootPdf")
        self.pdf_button.setMinimumHeight(36)
        self.pdf_button.clicked.connect(self._export_pdf_dialog)
        actions.addWidget(self.save_button)
        actions.addWidget(self.csv_button)
        actions.addWidget(self.pdf_button)
        actions.addStretch(1)
        self._body.addLayout(actions)

        ai_row = QHBoxLayout()
        self.explain_button = QPushButton("Explain this step")
        self.explain_button.setObjectName("troubleshootExplain")
        self.explain_button.setMinimumHeight(36)
        self.explain_button.clicked.connect(lambda: self.explain_now())
        self.explain_cancel = QPushButton("Cancel")
        self.explain_cancel.setObjectName("troubleshootExplainCancel")
        self.explain_cancel.setMinimumHeight(36)
        self.explain_cancel.clicked.connect(self.cancel_explain)
        self.explain_cancel.hide()
        settings = QPushButton("AI settings…")
        settings.setObjectName("troubleshootAiSettings")
        settings.setMinimumHeight(36)
        settings.clicked.connect(self.settings_requested.emit)
        ai_row.addWidget(self.explain_button)
        ai_row.addWidget(self.explain_cancel)
        ai_row.addWidget(settings)
        ai_row.addStretch(1)
        self._body.addLayout(ai_row)

        self.explain_busy = QProgressBar()
        self.explain_busy.setObjectName("troubleshootExplainBusy")
        self.explain_busy.setRange(0, 0)
        self.explain_busy.setTextVisible(False)
        self.explain_busy.setFixedHeight(8)
        self.explain_busy.hide()
        self._body.addWidget(self.explain_busy)

        self.explain_text = QLabel("")
        self.explain_text.setObjectName("troubleshootExplainText")
        self.explain_text.setWordWrap(True)
        self._body.addWidget(self.explain_text)
        self._body.addStretch(1)
        self.sync_explain()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_devices()
        self.sync_explain()

    def set_session(self, session) -> None:
        self._session = session
        self._marks.clear()
        self._guide = None
        self._load_devices()
        self._clear_cards()
        self.prompt.setText("Pick a symptom and a device, then show the checks.")
        self.progress.setText("")
        self.path_label.setText("")
        self.xref_label.setText("")
        self.fault.setText("")
        self.causes.setText("")
        self.sync_explain()
        self.highlight_requested.emit({"clear": True})

    def start(self) -> None:
        """Build the checks for the selected symptom and device."""

        self._marks.clear()
        self._rebuild()

    def mark(self, check_id: str, result: str) -> None:
        """Record OK, Wrong, or Skipped and move the guide.

        The rebuild waits until the button click has finished, so Qt does not
        destroy the button that is still handling the click.
        """

        self._marks[str(check_id)] = str(result)
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self._rebuild)

    def readings(self) -> list[dict[str, str]]:
        return [{"id": ident, "result": result} for ident, result in self._marks.items() if result]

    def current_guide(self) -> dict[str, Any] | None:
        return self._guide

    def save_log(self) -> dict[str, Any] | None:
        if self._session is None or self._guide is None:
            self._say("Show the checks before saving the log.")
            return None
        try:
            saved = self._session.append_troubleshoot_log(
                ref=str(self._guide["ref"]),
                symptom=str(self._guide["symptom"]),
                readings=self.readings() or None,
                who=self.who.text(),
                note=self.note.text(),
            )
        except QueryError as exc:
            self._say(str(exc))
            return None
        self._say(f"Saved the log beside the project ({saved.get('path', '')}).")
        return saved

    def write_csv(self, path: Path | str) -> Path:
        payload = self._require_guide()
        from panelcad.core.troubleshoot_guide import session_rows

        rows = session_rows(payload, who=self.who.text(), note=self.note.text())
        return write_session_csv(path, rows)

    def write_pdf(self, path: Path | str) -> Path:
        payload = self._require_guide()
        from panelcad.render.troubleshoot_log import write_session_pdf

        project = ""
        if self._session is not None:
            project = str(self._session.project.name or "")
        return write_session_pdf(
            path,
            payload,
            who=self.who.text(),
            note=self.note.text(),
            project_name=project,
        )

    def explain_now(self, transport=None) -> None:
        """Explain the current step off the UI thread, with the Settings > AI account."""

        from panelcad.ai.troubleshoot_explain import ExplainCall, explain_reason

        if self._explain_running:
            return
        reason = explain_reason()
        if reason:
            self.explain_text.setText(reason)
            self.sync_explain()
            return
        if self._session is None or self._guide is None:
            text = "Show the checks first, then explain this step."
            self.explain_text.setText(text)
            return
        self._explain_generation += 1
        generation = self._explain_generation
        call = ExplainCall()
        self._explain_call = call
        self._explain_running = True
        self._set_explain_busy(True)
        self.explain_text.setText("Explaining this step…")
        worker = _ExplainWorker(
            generation,
            self._session.project,
            self._session.catalog,
            self._guide,
            transport,
            call,
        )
        self._explain_worker = worker
        worker.finished_result.connect(
            self._explain_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.start()

    def cancel_explain(self) -> None:
        """Stop the in-flight explanation. A late reply is not shown."""

        if not self._explain_running:
            return
        call = self._explain_call
        if call is not None:
            call.cancel()
        self._explain_generation += 1
        self.explain_text.setText("Explain cancelled.")
        self.explain_cancel.hide()
        self.explain_busy.hide()

    def stop_explain(self) -> None:
        """Cancel and wait, so closing the window does not leave the worker running."""

        from PySide6.QtCore import QCoreApplication

        call = self._explain_call
        if call is not None:
            call.cancel()
        self._explain_generation += 1
        worker = self._explain_worker
        if worker is not None and worker.isRunning():
            worker.wait(1500)
        QCoreApplication.processEvents()
        self._explain_running = False
        self._set_explain_busy(False)

    def _explain_finished(self, generation: int, result: dict[str, Any]) -> None:
        worker = self._explain_worker
        if worker is not None:
            self._explain_worker = None
            worker.deleteLater()
        self._explain_running = False
        self._set_explain_busy(False)
        if generation != self._explain_generation:
            self.sync_explain()
            return
        if result.get("cancelled"):
            self.explain_text.setText("Explain cancelled.")
        else:
            shown = result.get("text") or result.get("reason") or ""
            self.explain_text.setText(str(shown))
        self.sync_explain()

    def _set_explain_busy(self, busy: bool) -> None:
        self.explain_busy.setVisible(busy)
        self.explain_cancel.setVisible(busy)
        if busy:
            self.explain_button.setEnabled(False)
            self.explain_button.setToolTip("Explaining this step…")

    def sync_explain(self) -> None:
        from panelcad.ai.troubleshoot_explain import explain_reason

        if self._explain_running:
            self.explain_button.setEnabled(False)
            self.explain_button.setToolTip("Explaining this step…")
            return
        reason = explain_reason()
        if reason:
            self.explain_button.setEnabled(False)
            self.explain_button.setToolTip(reason)
            if not self.explain_text.text():
                self.explain_text.setText(reason)
            return
        ready = self._guide is not None
        self.explain_button.setEnabled(ready)
        tip = (
            "Explain this step with your AI account from Settings > AI. "
            "PanelCAD does not include a key."
        )
        if not ready:
            tip = "Show the checks first. " + tip
        self.explain_button.setToolTip(tip)

    def _set_banner(self) -> None:
        from panelcad.core.troubleshoot import ARC_FLASH_TEXT, DRAWING_TEXT, LOTO_TEXT

        self.banner.setText(f"{LOTO_TEXT}\n{ARC_FLASH_TEXT}\n{DRAWING_TEXT}")

    def refresh_devices(self) -> None:
        """Reload tags from the drawing without dropping a guide that still applies."""

        if self._session is None:
            return
        current = self.device.currentData()
        self._fill_devices(keep=str(current) if current else "")
        if self._guide is None:
            return
        chosen = str(self.device.currentData() or "")
        if chosen != str(self._guide.get("ref") or ""):
            self._marks.clear()
            self._guide = None
            self._clear_cards()
            self.prompt.setText("Show the checks to follow this device.")
            self.highlight_requested.emit({"clear": True})

    def _load_devices(self) -> None:
        self._fill_devices(keep="")

    def _empty_clicked(self, ident: str) -> None:
        if ident == "help":
            self.help_requested.emit("troubleshoot")

    def _fill_devices(self, *, keep: str) -> None:
        self.device.blockSignals(True)
        self.device.clear()
        choices: list[dict[str, str]] = []
        if self._session is not None:
            choices = device_choices(self._session.project, self._session.catalog)
        for choice in choices:
            self.device.addItem(choice["label"], choice["ref"])
        if keep:
            index = self.device.findData(keep)
            if index >= 0:
                self.device.setCurrentIndex(index)
        self.device.blockSignals(False)
        from panelcad.guidance import troubleshoot_empty_copy

        message = troubleshoot_empty_copy(devices=len(choices))
        if message:
            self.empty.set_state(message, (("How this guide works", "help"),))
        else:
            self.empty.set_state("", ())
        self.start_button.setEnabled(bool(choices))
        if choices:
            self.start_button.setToolTip(
                "Build the checks from the wires and cross-references on this drawing."
            )
            self.intro.show()
        else:
            self.start_button.setToolTip("Add devices and wires on the drawing first.")
            self.intro.hide()

    def _on_picker_changed(self) -> None:
        if self._guide is None and not self._marks:
            return
        self._marks.clear()
        self._guide = None
        self._clear_cards()
        self.fault.setText("")
        self.causes.setText("")
        self.path_label.setText("")
        self.xref_label.setText("")
        self.progress.setText("")
        self.prompt.setText("Show the checks to follow this device.")
        self.highlight_requested.emit({"clear": True})
        self.sync_explain()

    def _rebuild(self) -> None:
        if self._session is None:
            return
        ref = self.device.currentData()
        symptom = self.symptom.currentData()
        if not ref or not symptom:
            self._say("Pick a symptom and a device.")
            return
        try:
            payload = guide(
                self._session.project,
                self._session.catalog,
                str(ref),
                str(symptom),
                self.readings() or None,
            )
        except QueryError as exc:
            self.prompt.setText(str(exc))
            self._say(str(exc))
            return
        self._guide = payload
        self._fill(payload)
        self._emit_highlight()
        self.sync_explain()

    def _fill(self, payload: dict[str, Any]) -> None:
        self.prompt.setText(str(payload.get("instruction") or ""))
        self.progress.setText(str(payload.get("progress") or ""))
        path = str(payload.get("path") or "")
        self.path_label.setText(f"Path: {path}" if path else "")
        lines = list(payload.get("cross_refs") or [])
        if lines:
            shown = lines[:6]
            extra = len(lines) - len(shown)
            text = "On the drawing:\n" + "\n".join(shown)
            if extra > 0:
                text += f"\n… and {extra} more"
            self.xref_label.setText(text)
        else:
            self.xref_label.setText("")
        fault = str(payload.get("fault_text") or "")
        if fault:
            self.fault.setText(fault)
        elif payload.get("checks"):
            self.fault.setText("No wrong reading yet. The fault window appears at the first Wrong.")
        else:
            self.fault.setText("")
        causes = list(payload.get("causes") or [])
        if causes:
            bits = []
            for row in causes[:8]:
                bits.append(f"{row.get('rank')}. {row.get('text')}")
            more = ""
            if len(causes) > 8:
                more = f"\n… and {len(causes) - 8} more"
            self.causes.setText("Likely causes:\n" + "\n".join(bits) + more)
        else:
            self.causes.setText("")
        self._build_cards(payload)

    def _build_cards(self, payload: dict[str, Any]) -> None:
        self._clear_cards()
        current = payload.get("next_check") or {}
        current_id = str(current.get("id") or "")
        layout = self._cards
        if layout is None:
            return
        for check in payload.get("checks") or []:
            layout.addWidget(self._card(check, current=check.get("id") == current_id))

    def _card(self, check: dict[str, Any], *, current: bool) -> QFrame:
        frame = QFrame()
        frame.setObjectName("troubleshootCurrent" if current else "troubleshootCheck")
        column = QVBoxLayout(frame)
        column.setContentsMargins(6, 6, 6, 6)
        order = check.get("order") or ""
        title = f"{order}. {check.get('energized') or ''}"
        if current:
            title = "Do this one — " + title
        head = QLabel(title)
        head.setFont(ui_font())
        column.addWidget(head)
        where = " ".join(
            part
            for part in (
                str(check.get("page_zone") or ""),
                str(check.get("device") or ""),
                f"terminal {check['terminal']}" if check.get("terminal") else "",
                f"wire {check['wire']}" if check.get("wire") else "",
            )
            if part
        )
        if where:
            place = QLabel(where)
            place.setWordWrap(True)
            column.addWidget(place)
        leads = str(check.get("meter_leads") or "")
        if leads:
            meter = QLabel(f"Meter: {leads}")
            meter.setWordWrap(True)
            column.addWidget(meter)
        expected = check.get("expected") or {}
        if expected.get("needs_info"):
            expect_text = "Expected: needs info — the drawing does not state this value."
        else:
            shown = expected.get("text") or expected.get("value") or ""
            expect_text = f"Expected: {shown}" if shown else ""
        if expect_text:
            expect = QLabel(expect_text)
            expect.setWordWrap(True)
            column.addWidget(expect)
        plc = check.get("plc") or {}
        if plc:
            plc_text = " ".join(
                part
                for part in (
                    "PLC",
                    str(plc.get("address") or ""),
                    str(plc.get("tag") or ""),
                    str(plc.get("direction") or ""),
                )
                if part
            )
            plc_label = QLabel(plc_text)
            plc_label.setWordWrap(True)
            column.addWidget(plc_label)
        detail = QLabel(str(check.get("instruction") or ""))
        detail.setWordWrap(True)
        column.addWidget(detail)
        buttons = QHBoxLayout()
        group = QButtonGroup(frame)
        group.setExclusive(True)
        check_id = str(check.get("id") or "")
        chosen = self._marks.get(check_id, "")
        for label, result, name in (
            ("OK", "ok", "Ok"),
            ("Wrong", "wrong", "Wrong"),
            ("Skipped", "skipped", "Skipped"),
        ):
            button = QPushButton(label)
            button.setObjectName(f"troubleshoot{name}_{order}")
            button.setCheckable(True)
            button.setMinimumHeight(36)
            button.setMinimumWidth(72)
            if chosen == result:
                button.setChecked(True)
            button.clicked.connect(
                lambda _checked=False, ident=check_id, value=result: self.mark(ident, value)
            )
            group.addButton(button)
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        return frame

    def _clear_cards(self) -> None:
        layout = self._cards
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _emit_highlight(self) -> None:
        if self._guide is None:
            self.highlight_requested.emit({"clear": True})
            return
        spec = dict(self._guide.get("highlight") or {})
        if spec:
            self.highlight_requested.emit(spec)

    def _require_guide(self) -> dict[str, Any]:
        if self._guide is None:
            raise QueryError("show the checks before exporting")
        return self._guide

    def _export_csv_dialog(self) -> None:
        if self._guide is None:
            self._say("Show the checks before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Troubleshooting log", "troubleshoot.csv", "CSV (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        dest = self.write_csv(path)
        self._say(f"troubleshooting CSV {dest}")

    def _export_pdf_dialog(self) -> None:
        if self._guide is None:
            self._say("Show the checks before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Troubleshooting log", "troubleshoot.pdf", "PDF (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        dest = self.write_pdf(path)
        self._say(f"troubleshooting PDF {dest}")

    def _say(self, text: str) -> None:
        self.status_message.emit(text)
