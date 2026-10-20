"""Wizard for a human to start a print import and open the review panel."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.importer.ingest import PRINT_FILE_FILTER
from panelcad.importer.pipeline import ImportJob
from panelcad.importer.vision import OpenAICompatibleVision, list_ollama_vision_models
from panelcad.gui.vision_settings import ModelBox, sync_ollama_think


def _review_crop(prep: Path | None, item: dict) -> QPixmap:
    """Crop the page PNG to the agent's bbox. Empty when the box or the file is missing."""

    if prep is None:
        return QPixmap()
    image = prep / str(item.get("image") or "")
    bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else None
    meta_path = image.with_suffix(".json")
    if bbox is None or not image.is_file() or not meta_path.is_file():
        return QPixmap()
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        width_mm = float(meta.get("width_mm") or 0)
        height_mm = float(meta.get("height_mm") or 0)
        x, y = float(bbox["x"]), float(bbox["y"])
        width, height = float(bbox["w"]), float(bbox["h"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return QPixmap()
    pix = QPixmap(str(image))
    if pix.isNull() or width_mm <= 0 or height_mm <= 0 or width <= 0 or height <= 0:
        return pix
    pad = 2.0
    x -= pad
    y -= pad
    width += pad * 2
    height += pad * 2
    scale_x = pix.width() / width_mm
    scale_y = pix.height() / height_mm
    x0 = max(0, int(x * scale_x))
    y0 = max(0, int(y * scale_y))
    x1 = min(pix.width(), int((x + width) * scale_x + 0.999))
    y1 = min(pix.height(), int((y + height) * scale_y + 0.999))
    if x1 <= x0 or y1 <= y0:
        return pix
    return pix.copy(x0, y0, x1 - x0, y1 - y0)


class _ImportThread(QThread):
    """Run the tile pool off the UI thread. Cancel kills the grok processes from the GUI thread."""

    tick = Signal(str)
    failed = Signal(str)
    ready = Signal()
    ask_cost = Signal(object)

    def __init__(self, job: ImportJob) -> None:
        super().__init__()
        self.job = job
        self.built = None

    def run(self) -> None:
        try:
            self.job._on_tick = self.tick.emit
            self.job.run()
            status = self.job.status()
            if not status.get("pending"):
                self.built = self.job.build()
            self.ready.emit()
        except Exception as exc:  # noqa: BLE001 — the wizard shows the message, not a traceback
            self.failed.emit(str(exc))


from panelcad.importer.vision_config import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    OLLAMA_WORKERS,
    PRESET_GROK_BUILD,
    PRESET_LABELS,
    PRESET_NONE,
    PRESET_OLLAMA,
    PRESETS,
    VisionSettings,
    load_settings,
)


def _page_status_line(row: dict) -> str:
    """Page id and kind. The raw kind ``other`` is left off."""

    kind = str(row.get("kind") or "").strip()
    if kind.lower() == "other":
        kind = ""
    text = str(row.get("id") or "")
    if kind:
        text = f"{text} {kind}"
    if row.get("standard"):
        text = f"{text} [{row['standard']}]"
    return text


def describe_import_result(
    *,
    name: str,
    pages: str,
    stage: str,
    fallbacks: int,
    devices: int,
    reasons: list[str],
    preset: str,
    project: str,
    wires: int | None = None,
    recovery: str = "",
) -> str:
    """Status text. 'Built' is only for a sheet that has devices, wires, and no fallbacks."""

    no_wires = wires is not None and int(wires) <= 0
    incomplete = stage in {"empty", "cancelled"} or int(devices) <= 0 or int(fallbacks) > 0 or no_wires
    if stage == "empty" or int(devices) <= 0:
        head = "Import did not build a sheet"
    elif incomplete:
        head = "Import finished"
    else:
        head = f"Built {name}"
    lines = [head, pages, project]
    if no_wires and int(devices) > 0 and stage not in {"empty", "cancelled"}:
        lines.append("No wires were built.")
    if recovery:
        lines.append(recovery)
    if reasons:
        lines.append("Fallbacks:")
        lines.extend(str(reason) for reason in reasons)
    if preset not in {"ollama", "grok-build", "none", ""}:
        lines.append(f"Backend {preset}")
    return "\n".join(line for line in lines if line)


class ImportWizard(QDialog):
    def __init__(self, parent=None, workspace: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import print")
        self.resize(720, 520)
        self.job: ImportJob | None = None
        self.session = None
        self._thread: _ImportThread | None = None
        self._closing = False
        self.transport = None

        self.files = QLineEdit()
        self.files.setPlaceholderText("PDF, images, DXF, or SVG")
        browse = QPushButton("Files…")
        browse.clicked.connect(self._pick_files)
        file_row = QHBoxLayout()
        file_row.addWidget(self.files)
        file_row.addWidget(browse)

        self.photos = QLineEdit()
        self.photos.setPlaceholderText("Optional folder of panel photos")
        self.photos.setToolTip(
            "Optional photos of the real panel, stored with this print. "
            "To build a drawing from photos only, use File → Reconstruct from photos."
        )
        photo_btn = QPushButton("Photos…")
        photo_btn.clicked.connect(self._pick_photos)
        photo_row = QHBoxLayout()
        photo_row.addWidget(self.photos)
        photo_row.addWidget(photo_btn)

        self.standard = QComboBox()
        self.standard.addItems(["auto", "IEC", "NEMA/ANSI"])
        self.standard.setToolTip(
            "Auto reads each page (title block, tags, rails, and symbols). "
            "IEC or NEMA/ANSI overrides that detection for every page."
        )
        self.workspace = QLineEdit(str(workspace or Path("import-workspace")))

        saved = load_settings()
        self.vision = QComboBox()
        self.vision.setObjectName("visionBackend")
        self.vision.setAccessibleName("Vision backend")
        for name in PRESETS:
            self.vision.addItem(PRESET_LABELS[name], name)
        index = self.vision.findData(saved.resolved().preset)
        self.vision.setCurrentIndex(max(0, index))
        self.vision.currentIndexChanged.connect(self._apply_vision_preset)
        resolved = saved.resolved()
        self.vision_model = ModelBox()
        self.vision_model.setObjectName("visionModel")
        self.vision_model.setAccessibleName("Vision model")
        self.vision_model.setText(resolved.model)
        self._vision_saved = saved
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setObjectName("testConnectionButton")
        self.test_btn.clicked.connect(self.test_connection)
        vision_row = QHBoxLayout()
        vision_row.addWidget(self.vision, stretch=1)
        vision_row.addWidget(self.vision_model, stretch=1)
        vision_row.addWidget(self.test_btn)
        self.workers = QSpinBox()
        self.workers.setObjectName("visionWorkers")
        self.workers.setRange(1, MAX_WORKERS)
        self.workers.setValue(resolved.workers)
        self.timeout = QDoubleSpinBox()
        self.timeout.setObjectName("visionTimeout")
        self.timeout.setRange(5, 1800)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(resolved.timeout_s)
        self.page_timeout = QDoubleSpinBox()
        self.page_timeout.setObjectName("visionPageTimeout")
        self.page_timeout.setRange(10, 14400)
        self.page_timeout.setSuffix(" s")
        self.page_timeout.setValue(resolved.page_timeout_s)
        self.num_ctx = QSpinBox()
        self.num_ctx.setObjectName("visionNumCtx")
        self.num_ctx.setRange(0, 131072)
        self.num_ctx.setSingleStep(1024)
        self.num_ctx.setValue(int(resolved.num_ctx or 0))
        self.num_predict = QSpinBox()
        self.num_predict.setObjectName("visionNumPredict")
        self.num_predict.setRange(0, 32768)
        self.num_predict.setSingleStep(100)
        self.num_predict.setValue(int(resolved.num_predict or 0))
        self.think = QCheckBox("Thinking")
        self.think.setObjectName("visionThink")
        self.think.setChecked(bool(resolved.think))
        sync_ollama_think(self.think, str(self.vision.currentData() or ""))
        self.effort = QComboBox()
        self.effort.setObjectName("visionReasoningEffort")
        self.effort.addItems(["", "none", "low", "medium", "high"])
        effort = self.effort.findText(resolved.reasoning_effort or "")
        self.effort.setCurrentIndex(max(0, effort))
        self.progress = QLabel("Tiles 0/0  fallbacks 0")
        self.progress.setObjectName("importProgress")

        self.outside = QCheckBox("Use an outside AI agent")
        self.outside.setObjectName("outsideAgent")
        self.outside.setToolTip(
            "Write page images and a JSON schema for an agent you run yourself. "
            "PanelCAD does not call a model and has no API key."
        )
        self.outside.toggled.connect(self._toggle_outside_agent)
        self.agent_dpi = QSpinBox()
        self.agent_dpi.setObjectName("agentDpi")
        self.agent_dpi.setRange(200, 300)
        self.agent_dpi.setValue(200)
        self.agent_dpi.setToolTip("Page PNG resolution. Tiles are crops of that PNG. 200–300 keeps a symbol readable.")
        self.agent_dpi.setEnabled(False)
        self._agent_folder: Path | None = None

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.agent_review = QScrollArea()
        self.agent_review.setObjectName("agentReview")
        self.agent_review.setWidgetResizable(True)
        self.agent_review_body = QWidget()
        self.agent_review_layout = QVBoxLayout(self.agent_review_body)
        self.agent_review.setWidget(self.agent_review_body)
        self.agent_review.setVisible(False)
        self._running = False

        self.run_btn = QPushButton("Start import")
        self.run_btn.clicked.connect(self.run_import)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelImportButton")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_import)
        self.review_btn = QPushButton("Open review")
        self.review_btn.setEnabled(False)
        self.review_btn.clicked.connect(self.accept)
        self.apply_btn = QPushButton("Apply agent JSON…")
        self.apply_btn.setObjectName("applyAgentJson")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply_agent_json)

        form = QFormLayout()
        form.addRow("Print", file_row)
        form.addRow("Panel photos (optional)", photo_row)
        form.addRow("Standard", self.standard)
        form.addRow("Vision", vision_row)
        form.addRow("Outside agent", self.outside)
        form.addRow("Agent DPI", self.agent_dpi)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setObjectName("importAdvanced")
        self.advanced_toggle.setText("Advanced (AI settings)")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_body = QWidget()
        self.advanced_body.setObjectName("importAdvancedBody")
        advanced_form = QFormLayout(self.advanced_body)
        advanced_form.addRow("Workers", self.workers)
        advanced_form.addRow("Request timeout", self.timeout)
        advanced_form.addRow("Page timeout", self.page_timeout)
        advanced_form.addRow("Context (num_ctx)", self.num_ctx)
        advanced_form.addRow("Completion (num_predict)", self.num_predict)
        advanced_form.addRow("Ollama thinking", self.think)
        advanced_form.addRow("Reasoning effort", self.effort)
        advanced_form.addRow("Progress", self.progress)
        advanced_form.addRow("Workspace", self.workspace)
        self.advanced_body.setVisible(False)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

        buttons = QHBoxLayout()
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(self.review_btn)

        from panelcad.gui.guidance import InlineNote
        from panelcad.gui.screen_fit import OptionScroll
        from panelcad.learn import print_import_note

        note = print_import_note()
        self.guide = InlineNote("print-import")
        self.guide.set_copy(note["title"], note["body"])
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.addWidget(self.guide)
        body_lay.addWidget(
            QLabel(
                "Vector lines and text, including SVG, are taken from the file. "
                "A scan uses the vision backend you pick."
            )
        )
        body_lay.addLayout(form)
        body_lay.addWidget(self.advanced_toggle)
        body_lay.addWidget(self.advanced_body)
        body_lay.addWidget(self.log, stretch=1)
        body_lay.addWidget(self.agent_review, stretch=1)
        scroll = OptionScroll()
        scroll.setObjectName("importWizardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addLayout(buttons)

    def _toggle_advanced(self, open_: bool) -> None:
        self.advanced_body.setVisible(bool(open_))
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        from panelcad.gui.screen_fit import fit_when_shown

        fit_when_shown(self)

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Print",
            "",
            PRINT_FILE_FILTER,
        )
        if paths:
            self.files.setText(";".join(paths))

    def _pick_photos(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Panel photos")
        if folder:
            self.photos.setText(folder)

    def source_paths(self) -> list[Path]:
        return [Path(part.strip()) for part in self.files.text().split(";") if part.strip()]

    def vision_settings(self) -> VisionSettings:
        name = str(self.vision.currentData() or PRESET_NONE)
        saved = self._vision_saved.resolved()
        return VisionSettings(
            preset=name,
            base_url=saved.base_url if name == saved.preset else "",
            model=self.vision_model.text().strip(),
            api_key_env=saved.api_key_env if name == saved.preset else "",
            timeout_s=float(self.timeout.value()),
            page_timeout_s=float(self.page_timeout.value()),
            max_usd=saved.max_usd,
            workers=int(self.workers.value()),
            grok_extra_args=saved.grok_extra_args,
            clean_grok_sessions=saved.clean_grok_sessions,
            grok_tools_none=saved.grok_tools_none,
            num_ctx=int(self.num_ctx.value()),
            num_predict=int(self.num_predict.value()),
            think=self.think.isChecked(),
            reasoning_effort=str(self.effort.currentText() or ""),
        ).resolved()

    def _apply_vision_preset(self) -> None:
        settings = VisionSettings(preset=str(self.vision.currentData() or PRESET_NONE)).resolved()
        self.vision_model.setText(settings.model)
        self.timeout.setValue(settings.timeout_s)
        self.page_timeout.setValue(settings.page_timeout_s)
        self.num_ctx.setValue(int(settings.num_ctx or 0))
        self.num_predict.setValue(int(settings.num_predict or 0))
        sync_ollama_think(self.think, settings.preset)
        self.effort.setCurrentIndex(max(0, self.effort.findText(settings.reasoning_effort or "")))
        if settings.preset == PRESET_OLLAMA and int(self.workers.value()) == DEFAULT_WORKERS:
            self.workers.setValue(OLLAMA_WORKERS)
        if settings.preset == PRESET_OLLAMA:
            self._refresh_models()

    def _refresh_models(self) -> None:
        current = self.vision_model.text().strip()
        base = self.vision_settings().base_url
        names = list_ollama_vision_models(base)
        self.vision_model.blockSignals(True)
        self.vision_model.clear()
        for name in names:
            self.vision_model.addItem(name)
        if current:
            self.vision_model.setText(current)
        elif names:
            self.vision_model.setText(names[0])
        self.vision_model.blockSignals(False)

    def test_connection(self) -> None:
        settings = self.vision_settings()
        if settings.preset == PRESET_GROK_BUILD:
            from panelcad.importer.grok_build import GrokBuildVision

            ok, message = GrokBuildVision(settings).test_connection()
        else:
            ok, message = OpenAICompatibleVision(settings).test_connection()
        prefix = "Connected. " if ok else "Not connected. "
        self.log.setPlainText(prefix + message)

    def cancel_import(self) -> None:
        if self.job is not None:
            self.job.cancel()
        self.log.append("Cancel requested. Remaining tiles keep the pixel read.")

    def _tick(self, message: str) -> None:
        if self._closing:
            return
        if message.startswith("tiles "):
            self.progress.setText("Tiles " + message[len("tiles ") :])
        self.log.append(message)

    def _import_failed(self, message: str) -> None:
        if self._closing:
            return
        self._running = False
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        reasons = []
        if self.job is not None:
            reasons = list((self.job.summary().get("fallback_reasons") or []))
        extra = ("\n" + "\n".join(str(reason) for reason in reasons)) if reasons else ""
        self.log.setPlainText(message + extra)
        items: list = []
        if self.job is not None:
            try:
                items = list(self.job.review_items() or [])
            except Exception:  # noqa: BLE001 — a failed file has no review to open
                items = []
        if items:
            self.review_btn.setEnabled(True)

    def _import_ready(self) -> None:
        if self._closing or self.job is None:
            return
        self._running = False
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        thread = self._thread
        self.session = thread.built if thread is not None else None
        if self.session is not None:
            self.job.stamp_rebuilt_previews(self.session)
        status = self.job.status()
        if self.job.manifest.get("cancelled"):
            done = int(self.job.manifest.get("cancelled_done") or 0)
            total = int(self.job.manifest.get("cancelled_total") or 0)
            label = f"Cancelled at {done}/{total}"
            self.progress.setText(label)
            self.log.append(label)
            for note in (status.get("summary") or {}).get("ocr_skipped") or []:
                self.log.append(str(note))
            if self.session is not None:
                self.review_btn.setEnabled(True)
            return
        if status.get("pending"):
            work = status.get("work") or {}
            self.log.setPlainText(
                "Waiting on one crop. An outside agent can submit import_submit for\n"
                f"{work.get('id')}\n{work.get('image')}"
            )
            self.review_btn.setEnabled(True)
            return
        if self.session is None:
            self.log.setPlainText("Import finished without a project.")
            return
        pages = ", ".join(_page_status_line(row) for row in status["pages"])
        summary = status.get("summary") or {}
        vision = status.get("vision") or {}
        devices = int(summary.get("devices") or 0) or sum(
            int(row.get("devices") or 0) for row in status.get("pages") or []
        )
        wires = sum(len(sheet.wires) for sheet in self.session.project.sheets)
        text = describe_import_result(
            name=self.session.project.name,
            pages=pages,
            stage=str(status.get("stage") or ""),
            fallbacks=int(summary.get("fallbacks") or 0),
            devices=devices,
            reasons=list(summary.get("fallback_reasons") or []),
            preset=str(vision.get("preset") or ""),
            project=str(self.job.manifest.get("project") or ""),
            wires=wires,
            recovery=str(summary.get("recovery") or ""),
        )
        notes = summary.get("ocr_skipped") or []
        if notes:
            text += "\n" + "\n".join(str(note) for note in notes)
        self.log.setPlainText(text)
        self.review_btn.setEnabled(True)

    def _vision_widgets(self) -> list:
        return [
            self.vision,
            self.vision_model,
            self.test_btn,
            self.workers,
            self.timeout,
            self.page_timeout,
            self.num_ctx,
            self.num_predict,
            self.think,
            self.effort,
        ]

    def _toggle_outside_agent(self, checked: bool) -> None:
        for widget in self._vision_widgets():
            widget.setEnabled(not checked)
        self.agent_dpi.setEnabled(checked)
        self.run_btn.setText("Prepare for an agent" if checked else "Start import")
        if not checked:
            self.apply_btn.setEnabled(False)

    def _prepare_outside_agent(self) -> None:
        from panelcad.importer.agent_exchange import prepare_print

        sources = self.source_paths()
        if not sources:
            self.log.setPlainText("Choose a PDF, image, or DXF.")
            return
        folder = Path(self.workspace.text().strip() or "import-workspace")
        try:
            payload = prepare_print(sources, folder, dpi=int(self.agent_dpi.value()))
        except (OSError, ValueError) as exc:
            self.log.setPlainText(str(exc).splitlines()[0])
            self.apply_btn.setEnabled(False)
            return
        self._agent_folder = folder
        readme = folder / "README.md"
        text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
        self.log.setPlainText(
            f"Prepared {payload['out']}\n"
            "Give this folder to an outside AI agent. PanelCAD does not call a model.\n"
            "When the agent returns JSON, choose Apply agent JSON.\n\n"
            + text
        )
        self.apply_btn.setEnabled(True)

    def _apply_agent_json(self) -> None:
        from panelcad.api.session import Session
        from panelcad.importer.agent_exchange import apply_response

        path, _selected = QFileDialog.getOpenFileName(self, "Agent JSON", "", "JSON (*.json);;All files (*.*)")
        if not path:
            return
        out = Path(self.workspace.text().strip() or "import-workspace") / "project.pcad.yaml"
        report = apply_response(path, out=out, prep=self._agent_folder)
        counts = report.get("counts") or {}
        questions = report.get("questions") or []
        self.log.setPlainText(
            "connected {connected}  needs-review {needs_review}  rejected {rejected}  questions {questions}\n\n".format(
                connected=counts.get("connected", 0),
                needs_review=counts.get("needs_review", 0),
                rejected=counts.get("rejected", 0),
                questions=len(questions),
            )
            + json.dumps(report, indent=2, default=str)
        )
        if report.get("ok"):
            try:
                self.session = Session.open(report["project"])
            except (OSError, ValueError) as exc:
                self.log.append(str(exc).splitlines()[0])
                return
            self.review_btn.setText("Open project")
            self.review_btn.setEnabled(True)
        self._show_questions(questions)

    def _show_questions(self, rows: list) -> None:
        self._question_rows = [row for row in rows if isinstance(row, dict)]
        self._question_index = 0
        self._render_question()

    def _render_question(self) -> None:
        while self.agent_review_layout.count():
            item = self.agent_review_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        rows = getattr(self, "_question_rows", [])
        self.agent_review.setVisible(bool(rows))
        if not rows:
            return
        row = rows[self._question_index % len(rows)]
        image = QLabel()
        image.setPixmap(_review_crop(self._agent_folder, row))
        self.agent_review_layout.addWidget(image)
        title = QLabel(f"Question {self._question_index % len(rows) + 1} of {len(rows)}")
        title.setObjectName("agentQuestionCount")
        self.agent_review_layout.addWidget(title)
        question = QLabel(str(row.get("question") or ""))
        question.setObjectName("agentQuestion")
        question.setWordWrap(True)
        self.agent_review_layout.addWidget(question)
        changes = QLabel(str(row.get("changes") or ""))
        changes.setWordWrap(True)
        self.agent_review_layout.addWidget(changes)
        self.agent_answer = QLineEdit()
        self.agent_answer.setObjectName("agentAnswer")
        self.agent_review_layout.addWidget(self.agent_answer)
        for candidate in row.get("candidates") or []:
            button = QPushButton(str(candidate))
            button.setObjectName("agentCandidate")
            button.clicked.connect(lambda _checked=False, text=str(candidate): self.agent_answer.setText(text))
            self.agent_review_layout.addWidget(button)
        actions = QHBoxLayout()
        apply = QPushButton("Apply answer")
        apply.setObjectName("agentApplyAnswer")
        apply.clicked.connect(self._apply_current_answer)
        skip = QPushButton("Skip")
        skip.setObjectName("agentSkipQuestion")
        skip.clicked.connect(self._skip_question)
        actions.addWidget(apply)
        actions.addWidget(skip)
        host = QWidget()
        host.setLayout(actions)
        self.agent_review_layout.addWidget(host)

    def _apply_current_answer(self) -> None:
        from panelcad.importer.agent_questions import apply_answer

        rows = getattr(self, "_question_rows", [])
        if self.session is None or not self.session.path or not rows:
            return
        row = rows[self._question_index % len(rows)]
        result = apply_answer(self.session, str(row.get("id") or ""), self.agent_answer.text())
        if not result.get("applied"):
            self.log.append(str(result.get("error") or "answer was not applied"))
            return
        self._show_questions(result.get("questions") or [])

    def _skip_question(self) -> None:
        rows = getattr(self, "_question_rows", [])
        if len(rows) < 2:
            return
        self._question_index = (self._question_index + 1) % len(rows)
        self._render_question()

    def run_import(self) -> None:
        if self.outside.isChecked():
            self._prepare_outside_agent()
            return
        if self._running:
            return
        sources = self.source_paths()
        if not sources:
            self.log.setPlainText("Choose a PDF, image, DXF, or SVG.")
            return
        standard = self.standard.currentText()
        if standard == "auto":
            standard = ""
        photos = self.photos.text().strip()
        settings = self.vision_settings()
        self._running = True
        self.cancel_btn.setEnabled(True)
        self.run_btn.setEnabled(False)
        try:
            self.job = ImportJob.prepare(
                sources,
                self.workspace.text().strip() or "import-workspace",
                photos=photos or None,
                standard=standard or None,
                backend="raster" if settings.preset == PRESET_NONE else settings.preset,
                vision=settings,
                transport=self.transport,
            )
        except Exception as exc:  # noqa: BLE001 — the wizard shows the failure
            self.log.setPlainText(str(exc))
            self._running = False
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            return
        self._cost_done = threading.Event()
        self._cost_ok = False
        self.job._on_cost_guard = self._ask_cost_guard
        self._thread = _ImportThread(self.job)
        self._thread.tick.connect(self._tick)
        self._thread.failed.connect(self._import_failed)
        self._thread.ready.connect(self._import_ready)
        self._thread.ask_cost.connect(self._answer_cost_guard)
        self._thread.start()

    def _ask_cost_guard(self, info: dict) -> bool:
        """Worker thread: ask the GUI whether to read past the page cost cap."""

        if QThread.currentThread() is self.thread():
            return self._confirm_cost_guard(info)
        self._cost_done.clear()
        self._cost_ok = False
        thread = self._thread
        if thread is None:
            return False
        thread.ask_cost.emit(dict(info))
        self._cost_done.wait(120)
        return bool(self._cost_ok)

    def _answer_cost_guard(self, info: object) -> None:
        payload = info if isinstance(info, dict) else {}
        self._cost_ok = self._confirm_cost_guard(payload)
        done = getattr(self, "_cost_done", None)
        if done is not None:
            done.set()

    def _confirm_cost_guard(self, info: dict) -> bool:
        spent = float(info.get("spent_usd") or 0)
        cap = float(info.get("max_usd") or 0)
        remaining = int(info.get("remaining_tiles") or 0)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Page cost guard")
        box.setText(
            f"This read has spent ${spent:.2f} of the ${cap:.2f} limit.\n"
            f"{remaining} tiles have not been sent.\n"
            "Continue and read the remaining tiles?"
        )
        box.addButton("Skip remaining", QMessageBox.ButtonRole.RejectRole)
        go = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(go)
        box.exec()
        return box.clickedButton() is go

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self.cancel_import()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.wait(3000)
        super().closeEvent(event)
