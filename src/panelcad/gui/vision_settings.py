"""Settings for the scan vision backend. The API key is never written here."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from panelcad.importer.vision import OpenAICompatibleVision, list_ollama_vision_models
from panelcad.importer.vision_config import (
    DEFAULT_WORKERS,
    GROK_CLI_DOC,
    GROK_HEADLESS_DOC,
    MAX_WORKERS,
    OLLAMA_WORKERS,
    PRESET_GROK,
    PRESET_GROK_BUILD,
    PRESET_LABELS,
    PRESET_NONE,
    PRESET_OLLAMA,
    PRESETS,
    XAI_ENDPOINT_DOC,
    XAI_MODEL,
    XAI_MODEL_DOC,
    VisionSettings,
    load_settings,
    preset_defaults,
    save_settings,
)


THINK_TOOLTIP = "Only applies to Ollama."


def sync_ollama_think(box: QCheckBox, preset: str) -> None:
    """The thinking flag is an Ollama option. Other presets leave the saved check alone."""

    box.setToolTip(THINK_TOOLTIP)
    box.setEnabled(str(preset or "").strip().lower() == PRESET_OLLAMA)


class ModelBox(QComboBox):
    """Editable model id. ``text`` and ``setText`` match the line edit the tests already use."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        edit = self.lineEdit()
        if edit is not None:
            edit.setPlaceholderText("Model id")

    def text(self) -> str:
        return self.currentText()

    def setText(self, value: str) -> None:
        value = str(value or "")
        if value and self.findText(value) < 0:
            self.addItem(value)
        self.setCurrentText(value)


class VisionSettingsDialog(QDialog):
    """Choose none, xAI Grok, a local OpenAI-compatible server, or a custom endpoint."""

    def __init__(self, parent=None, path: Path | str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scan vision")
        self.resize(720, 520)
        self._path = Path(path) if path else None
        self.settings = load_settings(self._path)

        self.preset = QComboBox()
        self.preset.setObjectName("visionPreset")
        self.preset.setAccessibleName("Vision backend")
        for name in PRESETS:
            self.preset.addItem(PRESET_LABELS[name], name)
        self.preset.currentIndexChanged.connect(self._apply_preset)

        self.base_url = QLineEdit()
        self.base_url.setObjectName("visionBaseUrl")
        self.model = ModelBox()
        self.model.setObjectName("visionModel")
        self.model.setAccessibleName("Vision model")
        self.key_env = QLineEdit()
        self.key_env.setObjectName("visionKeyEnv")
        self.key_env.setPlaceholderText("Environment variable name")

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(5, 1800)
        self.timeout.setSuffix(" s")
        self.page_timeout = QDoubleSpinBox()
        self.page_timeout.setRange(10, 14400)
        self.page_timeout.setSuffix(" s")
        self.num_ctx = QSpinBox()
        self.num_ctx.setRange(0, 131072)
        self.num_ctx.setSingleStep(1024)
        self.num_ctx.setObjectName("visionNumCtx")
        self.num_predict = QSpinBox()
        self.num_predict.setRange(0, 32768)
        self.num_predict.setSingleStep(100)
        self.num_predict.setObjectName("visionNumPredict")
        self.think = QCheckBox("Thinking")
        self.think.setObjectName("visionThink")
        self.effort = QComboBox()
        self.effort.setObjectName("visionReasoningEffort")
        self.effort.addItems(["", "none", "low", "medium", "high"])
        self.max_usd = QDoubleSpinBox()
        self.max_usd.setRange(0.05, 50)
        self.max_usd.setDecimals(2)
        self.max_usd.setPrefix("$")
        self.workers = QSpinBox()
        self.workers.setObjectName("visionWorkers")
        self.workers.setRange(1, MAX_WORKERS)
        self.workers.setValue(3)
        self.extra = QLineEdit()
        self.extra.setObjectName("grokExtraArgs")
        self.extra.setPlaceholderText("Optional grok flags. Do not put a credential here.")
        self.clean_sessions = QCheckBox("Delete each tile's Grok session when it finishes")
        self.clean_sessions.setObjectName("grokCleanSessions")
        self.clean_sessions.setChecked(True)
        self.tools_none = QCheckBox("Also pass --tools none (off until tested on this PC)")
        self.tools_none.setObjectName("grokToolsNone")
        self.tools_none.setChecked(False)

        self.docs = QLabel()
        self.docs.setWordWrap(True)
        self.status = QLabel("The API key is read from the environment. It is not saved.")
        self.status.setWordWrap(True)

        self.test_btn = QPushButton("Test connection")
        self.test_btn.setObjectName("testConnectionButton")
        self.test_btn.clicked.connect(self.test_connection)

        form = QFormLayout()
        form.addRow("Backend", self.preset)
        form.addRow("Base URL", self.base_url)
        form.addRow("Model", self.model)
        form.addRow("API key variable", self.key_env)
        form.addRow("Request timeout", self.timeout)
        form.addRow("Page timeout", self.page_timeout)
        form.addRow("Context (num_ctx)", self.num_ctx)
        form.addRow("Completion (num_predict)", self.num_predict)
        form.addRow("Ollama thinking", self.think)
        form.addRow("Reasoning effort", self.effort)
        form.addRow("Page cost guard", self.max_usd)
        form.addRow("Workers", self.workers)
        form.addRow("Grok CLI extra", self.extra)
        form.addRow("Grok sessions", self.clean_sessions)
        form.addRow("Grok tools", self.tools_none)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.test_btn)
        row.addStretch(1)
        row.addWidget(buttons)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Scanned pages use this backend. Vector PDFs are still read from the file."))
        lay.addLayout(form)
        lay.addWidget(self.docs)
        lay.addWidget(self.status)
        lay.addStretch(1)
        lay.addLayout(row)
        self._load(self.settings)

    def current_settings(self) -> VisionSettings:
        name = str(self.preset.currentData() or PRESET_NONE)
        return VisionSettings(
            preset=name,
            base_url=self.base_url.text().strip(),
            model=self.model.text().strip(),
            api_key_env=self.key_env.text().strip(),
            timeout_s=float(self.timeout.value()),
            page_timeout_s=float(self.page_timeout.value()),
            max_usd=float(self.max_usd.value()),
            workers=int(self.workers.value()),
            grok_extra_args=self.extra.text().strip(),
            clean_grok_sessions=self.clean_sessions.isChecked(),
            grok_tools_none=self.tools_none.isChecked(),
            num_ctx=int(self.num_ctx.value()),
            num_predict=int(self.num_predict.value()),
            think=self.think.isChecked(),
            reasoning_effort=str(self.effort.currentText() or ""),
        ).resolved()

    def test_connection(self) -> None:
        settings = self.current_settings()
        if settings.preset == PRESET_GROK_BUILD:
            from panelcad.importer.grok_build import GrokBuildVision

            ok, message = GrokBuildVision(settings).test_connection()
        else:
            ok, message = OpenAICompatibleVision(settings).test_connection()
        self.status.setText(("Connected. " if ok else "Not connected. ") + message)

    def _load(self, settings: VisionSettings) -> None:
        item = settings.resolved()
        index = self.preset.findData(item.preset)
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(max(0, index))
        self.preset.blockSignals(False)
        self.base_url.setText(item.base_url)
        self.model.setText(item.model)
        self.key_env.setText(item.api_key_env)
        self.timeout.setValue(item.timeout_s)
        self.page_timeout.setValue(item.page_timeout_s)
        self.num_ctx.setValue(int(item.num_ctx or 0))
        self.num_predict.setValue(int(item.num_predict or 0))
        self.think.setChecked(bool(item.think))
        sync_ollama_think(self.think, item.preset)
        effort = self.effort.findText(item.reasoning_effort or "")
        self.effort.setCurrentIndex(max(0, effort))
        self.max_usd.setValue(item.max_usd)
        self.workers.setValue(item.workers)
        self.extra.setText(item.grok_extra_args)
        self.clean_sessions.setChecked(bool(item.clean_grok_sessions))
        self.tools_none.setChecked(bool(item.grok_tools_none))
        self._show_docs(item.preset)

    def _apply_preset(self) -> None:
        name = str(self.preset.currentData() or PRESET_NONE)
        defaults = preset_defaults(name)
        self.base_url.setText(defaults.base_url)
        self.model.setText(defaults.model)
        self.key_env.setText(defaults.api_key_env)
        if name in {PRESET_GROK_BUILD, PRESET_OLLAMA}:
            self.timeout.setValue(defaults.timeout_s)
            self.page_timeout.setValue(defaults.page_timeout_s)
        self.num_ctx.setValue(int(defaults.num_ctx or 0))
        self.num_predict.setValue(int(defaults.num_predict or 0))
        sync_ollama_think(self.think, name)
        self.effort.setCurrentIndex(max(0, self.effort.findText(defaults.reasoning_effort or "")))
        if name == PRESET_OLLAMA and int(self.workers.value()) == DEFAULT_WORKERS:
            self.workers.setValue(OLLAMA_WORKERS)
        if name == PRESET_OLLAMA:
            self._refresh_models()
        self._show_docs(name)

    def _refresh_models(self) -> None:
        """Fill the model box from Ollama, keeping whatever is already typed."""

        current = self.model.text().strip()
        names = list_ollama_vision_models(self.base_url.text().strip())
        self.model.blockSignals(True)
        self.model.clear()
        for name in names:
            self.model.addItem(name)
        if current:
            self.model.setText(current)
        elif names:
            self.model.setText(names[0])
        self.model.blockSignals(False)

    def _show_docs(self, preset: str) -> None:
        if preset == PRESET_GROK:
            self.docs.setText(
                f"Grok preset uses {XAI_MODEL}.\n"
                f"Model: {XAI_MODEL_DOC}\n"
                f"Endpoint: {XAI_ENDPOINT_DOC}"
            )
            return
        if preset == PRESET_GROK_BUILD:
            self.docs.setText(
                "Local Grok Build runs grok headless with --prompt-file, --json-schema, "
                "--output-format json, --effort low, --max-turns 5, --no-plan, and a short "
                "--system-prompt-override. "
                "Tool calls are refused with --permission-mode dontAsk. "
                "Each tile denies Bash, Read, Grep, Glob, Edit, Write, WebFetch, WebSearch, "
                "and MCPTool. The image is inline, so the model does not read a file. "
                "Before the tiles, one grok inspect writes a temporary project config that "
                "disables every enabled plugin and every config.toml MCP server, including "
                "Blender, so those processes do not start. If inspect fails, they may still "
                "start. No --no-mcp flag exists. "
                "--tools none is optional and stays off until it is tested on this PC. "
                "The xAI API backend is the cheapest run. "
                "Page timeout is the total page budget, including OCR and retries. "
                "Each tile waits 600 s, then one smaller-image retry, then falls back. "
                "Token counts are in the summary. "
                "The session folder is ~/.grok/sessions/<encoded cwd>/<id> and is removed "
                "when that option is on. "
                "Sign in with grok login. PanelCAD does not store credentials.\n"
                f"{GROK_CLI_DOC}\n{GROK_HEADLESS_DOC}"
            )
            return
        if preset == PRESET_NONE:
            self.docs.setText("Pixel-only. No model is called.")
            return
        if preset == PRESET_OLLAMA:
            self.docs.setText(
                "Ollama uses native POST /api/chat, not the OpenAI /v1 path. "
                "The default model is gemma3:4b. Thinking is off. num_ctx is 16384. "
                "num_predict is 4096. One worker is the default, because a large local model queues behind itself. "
                "One request waits 300 s. The page clock covers that page only and waits 3600 s. "
                "The model list is the vision-capable tags from /api/tags and /api/show. "
                "Install Ollama, then: ollama pull gemma3:4b. "
                "gemma3:4b needs about 8 GB. qwen3-vl:8b-instruct needs about 16 GB. "
                "qwen3.8:27b spills onto the CPU on a 16 GB GPU; leave thinking off."
            )
            return
        self.docs.setText(
            "OpenAI-compatible POST {base}/chat/completions. "
            "Custom servers get max_tokens. Reasoning effort is sent when it is set."
        )

    def _save(self) -> None:
        self.settings = self.current_settings()
        save_settings(self.settings, self._path)
        self.accept()
