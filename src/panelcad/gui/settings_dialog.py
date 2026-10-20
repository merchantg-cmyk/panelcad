"""Settings. Appearance, recovery, bug reports, and AI share one file.

Appearance applies as soon as a control changes. Recovery, bug reports, and
AI datasheet lookup save together. The interval is ``autosave_seconds``.
Slack fields are ``bug_reports.webhook_url``, ``slack_bot_token``,
``slack_channel``, ``relay_url``, and ``disabled``. A release may embed the
team webhook. The AI key is the one typed here. PanelCAD does not ship one.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.diag.settings import (
    ENV_REPORTS,
    BugSettings,
    autosave_env_set,
    load_stored_settings,
    save_settings,
    set_autosave_seconds,
    stored_autosave_seconds,
)
from panelcad.gui.bug_report_dialog import TEST_TIP, _send_notice, _wrapping
from panelcad.theme import (
    MARGIN_PX,
    SPACING_PX,
    canvas_hex,
    get_canvas,
    get_canvas_choice,
    get_theme,
    phase_colors_enabled,
    set_canvas,
    set_phase_colors,
    set_theme,
    ui_hex,
)


def _ai_env_notes() -> str:
    import os

    notes = []
    if os.environ.get("PANELCAD_AI_LOOKUP", "").strip():
        notes.append("PANELCAD_AI_LOOKUP is set and overrides the checkbox.")
    if os.environ.get("PANELCAD_AI_PROVIDER", "").strip():
        notes.append("PANELCAD_AI_PROVIDER is set and overrides the provider.")
    if os.environ.get("PANELCAD_AI_ENDPOINT", "").strip() or os.environ.get("PANELCAD_AI_BASE_URL", "").strip():
        notes.append("PANELCAD_AI_ENDPOINT is set and overrides the endpoint.")
    if os.environ.get("PANELCAD_AI_API_KEY", "").strip():
        notes.append("PANELCAD_AI_API_KEY is set and overrides the saved key.")
    if os.environ.get("PANELCAD_AI_MODEL", "").strip():
        notes.append("PANELCAD_AI_MODEL is set and overrides the model.")
    if os.environ.get("PANELCAD_WEB_ACCESS", "").strip().lower() in {"0", "off", "false", "no"}:
        notes.append("PANELCAD_WEB_ACCESS is off, so datasheet lookup stays off.")
    return "\n".join(notes)


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, session: Session | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setObjectName("settingsDialog")
        self.setMinimumSize(480, 360)
        self._session = session if isinstance(session, Session) else getattr(parent, "session", None)
        if not isinstance(self._session, Session):
            self._session = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        tabs = QTabWidget()
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._recovery_tab(), "Autosave")
        tabs.addTab(self._reports_tab(), "Bug reports")
        tabs.addTab(self._ai_tab(), "AI")
        layout.addWidget(tabs, 1)
        note = QLabel(
            "Match Windows follows the operating system color scheme. "
            "Print, PDF, DXF, and SVG stay on white paper with the drawing colors. "
            "Autosave copies unsaved work into the recovery folder. "
            "Your project file is never the destination."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._load_appearance()
        self.theme.currentIndexChanged.connect(self._apply_theme)
        self.canvas.currentIndexChanged.connect(self._apply_canvas)
        self.phase.toggled.connect(self._apply_phase)
        self.hints.toggled.connect(self._apply_hints)
        for field in (self.webhook, self.token, self.channel):
            field.textChanged.connect(self._refresh_test)
        self.reports_off.toggled.connect(self._refresh_test)
        self._refresh_test()

    def _appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        form = QFormLayout()
        form.setSpacing(SPACING_PX)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.theme = QComboBox()
        self.theme.addItem("Light", "light")
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Match Windows", "system")
        self.canvas = QComboBox()
        self.canvas.addItem("Paper", "paper")
        self.canvas.addItem("White", "white")
        self.canvas.addItem("Dark", "dark")
        self.canvas.addItem("Auto (follow theme)", "auto")
        self.phase = QCheckBox("Phase colors on screen")
        self.phase.setToolTip("Screen only. Print, PDF, DXF, and SVG keep their own strokes.")
        self.hints = QCheckBox("Show one-time tips")
        self.hints.setObjectName("showHints")
        self.hints.setToolTip("The first visit to a tool shows one short tip. Turn this off to hide them all.")
        form.addRow("Theme", self.theme)
        form.addRow("Sheet canvas", self.canvas)
        form.addRow("", self.phase)
        form.addRow("", self.hints)
        layout.addLayout(form)
        self.swatch = QFrame()
        self.swatch.setFixedHeight(72)
        self.swatch.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.swatch)
        layout.addStretch(1)
        return page

    def _recovery_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        stored_interval = stored_autosave_seconds()
        self.autosave = QSpinBox()
        self.autosave.setObjectName("autosaveSeconds")
        self.autosave.setRange(0, max(24 * 60 * 60, stored_interval))
        self.autosave.setValue(stored_interval)
        self.autosave.setSuffix(" s")
        self.autosave.setToolTip("0 turns autosave off")
        box = QGroupBox("Recovery")
        form = QFormLayout(box)
        form.addRow(
            _wrapping(
                "Copies unsaved work into the recovery folder. "
                "0 turns autosave off. Your project file is never the destination."
            )
        )
        form.addRow("Autosave interval", self.autosave)
        if autosave_env_set():
            form.addRow(_wrapping("PANELCAD_AUTOSAVE_SECONDS is set and overrides the saved interval."))
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _reports_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        stored = load_stored_settings()
        overrides = stored.env_overrides()
        self.webhook = QLineEdit(stored.webhook_url)
        self.webhook.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook.setPlaceholderText("https://hooks.slack.com/services/…")
        self.token = QLineEdit(stored.slack_bot_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("xoxb-…")
        self.channel = QLineEdit(stored.slack_channel)
        self.channel.setPlaceholderText("Slack channel id")
        self.reveal = QCheckBox("Show webhook and token")
        self.reveal.toggled.connect(self._toggle_secrets)
        self.reports_off = QCheckBox("Turn off bug reports")
        self.reports_off.setObjectName("bugReportsOff")
        self.reports_off.setChecked(stored.disabled)
        self.builtin = QLabel("Using built-in PanelCAD team channel")
        self.builtin.setObjectName("builtinChannel")
        self.builtin.setVisible(False)
        box = QGroupBox("Bug reports")
        form = QFormLayout(box)
        form.addRow(
            _wrapping(
                "A shop release can include a bot that uploads the screenshot and the zip. "
                "That bot stays on the channel id baked in at build time. "
                "A public build includes neither a bot nor a webhook. "
                "A webhook saved here, or PANELCAD_BUG_WEBHOOK, overrides the webhook. "
                "PANELCAD_SLACK_BOT_TOKEN and PANELCAD_SLACK_CHANNEL override a saved bot. "
                "They do not retarget the built-in bot. "
                "PANELCAD_BUG_REPORTS=off turns sending off. "
                "The zip stays on this computer. If the upload does not post, the webhook "
                "posts the report text."
            )
        )
        form.addRow(self.builtin)
        form.addRow("Webhook URL", self.webhook)
        form.addRow("Bot token", self.token)
        form.addRow("Channel id", self.channel)
        form.addRow("", self.reveal)
        form.addRow("", self.reports_off)
        notes = []
        if overrides["webhook_url"]:
            notes.append("PANELCAD_BUG_WEBHOOK is set and overrides the saved webhook.")
        if overrides["slack_bot_token"]:
            notes.append("PANELCAD_SLACK_BOT_TOKEN is set and overrides the saved token.")
        if overrides["slack_channel"]:
            notes.append("PANELCAD_SLACK_CHANNEL is set and overrides the saved channel.")
        if overrides["disabled"]:
            notes.append(f"{ENV_REPORTS}=off turns sending off.")
        if notes:
            form.addRow(_wrapping("\n".join(notes)))
        layout.addWidget(box)
        self.test_btn = QPushButton("Send test report")
        self.test_btn.setObjectName("sendTestReport")
        self.test_btn.clicked.connect(self._test)
        layout.addWidget(self.test_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _ai_tab(self) -> QWidget:
        from panelcad.ai.config import load_ai_lookup

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        stored = load_ai_lookup(stored_only=True)
        self.ai_lookup = QCheckBox("AI datasheet lookup")
        self.ai_lookup.setObjectName("aiDatasheetLookup")
        self.ai_lookup.setChecked(stored.enabled)
        self.ai_lookup.setToolTip(
            "Look up a datasheet with your own AI account. "
            "Proposals stay proposals until you accept them."
        )
        self.ai_provider = QComboBox()
        self.ai_provider.setObjectName("aiProvider")
        self.ai_provider.addItem("Choose", "")
        self.ai_provider.addItem("xAI", "xai")
        self.ai_provider.addItem("OpenAI", "openai")
        self.ai_provider.addItem("OpenAI-compatible", "compatible")
        self.ai_provider.addItem("Ollama", "ollama")
        provider_index = self.ai_provider.findData(stored.provider)
        self.ai_provider.setCurrentIndex(provider_index if provider_index >= 0 else 0)
        self.ai_endpoint = QLineEdit(stored.endpoint)
        self.ai_endpoint.setObjectName("aiEndpoint")
        self.ai_endpoint.setPlaceholderText("https://api.example.com/v1")
        self.ai_key = QLineEdit(stored.api_key)
        self.ai_key.setObjectName("aiApiKey")
        self.ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key.setPlaceholderText("Your API key")
        self.ai_show_key = QCheckBox("Show API key")
        self.ai_show_key.setObjectName("aiShowKey")
        self.ai_show_key.toggled.connect(self._toggle_ai_key)
        self.ai_model = QLineEdit(stored.model)
        self.ai_model.setObjectName("aiModel")
        self.ai_model.setPlaceholderText("optional model name")
        box = QGroupBox("AI")
        form = QFormLayout(box)
        form.addRow(
            _wrapping(
                "Datasheet lookup uses the account you connect here: xAI, OpenAI, "
                "an OpenAI-compatible endpoint, or local Ollama. "
                "PanelCAD does not include an API key. "
                "PANELCAD_AI_LOOKUP, PANELCAD_AI_PROVIDER, PANELCAD_AI_ENDPOINT, "
                "PANELCAD_AI_API_KEY, and PANELCAD_AI_MODEL override these fields. "
                "PANELCAD_WEB_ACCESS=off turns the lookup off."
            )
        )
        form.addRow("", self.ai_lookup)
        form.addRow("Provider", self.ai_provider)
        form.addRow("Endpoint", self.ai_endpoint)
        form.addRow("API key", self.ai_key)
        form.addRow("", self.ai_show_key)
        form.addRow("Model", self.ai_model)
        notes = _ai_env_notes()
        if notes:
            self.ai_env = QLabel(notes)
            self.ai_env.setObjectName("aiEnvNote")
            self.ai_env.setWordWrap(True)
            form.addRow(self.ai_env)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _toggle_ai_key(self, show: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self.ai_key.setEchoMode(mode)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._refresh_test()

    def _load_appearance(self) -> None:
        from panelcad.guidance import hints_enabled

        self.theme.blockSignals(True)
        self.canvas.blockSignals(True)
        self.phase.blockSignals(True)
        self.hints.blockSignals(True)
        theme_index = self.theme.findData(get_theme())
        canvas_index = self.canvas.findData(get_canvas_choice())
        self.theme.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        self.canvas.setCurrentIndex(canvas_index if canvas_index >= 0 else 0)
        self.phase.setChecked(phase_colors_enabled())
        self.hints.setChecked(hints_enabled())
        self.theme.blockSignals(False)
        self.canvas.blockSignals(False)
        self.phase.blockSignals(False)
        self.hints.blockSignals(False)
        self._paint_swatch()

    def _apply_theme(self) -> None:
        self._apply(lambda: set_theme(str(self.theme.currentData())))
        self._paint_swatch()

    def _apply_canvas(self) -> None:
        self._apply(lambda: set_canvas(str(self.canvas.currentData())))
        self._paint_swatch()

    def _apply_phase(self, checked: bool) -> None:
        self._apply(lambda: set_phase_colors(bool(checked)))

    def _apply_hints(self, checked: bool) -> None:
        from panelcad.guidance import set_hints_enabled

        self._apply(lambda: set_hints_enabled(bool(checked)))

    def _apply(self, write) -> None:
        try:
            write()
        except OSError as exc:
            self._load_appearance()
            from panelcad.gui import bug_report_dialog as dialogs

            dialogs.QMessageBox.warning(self, "Settings", f"could not save settings: {exc}")

    def _paint_swatch(self) -> None:
        paper = canvas_hex("paper", get_canvas())
        ink = canvas_hex("ink", get_canvas())
        window = ui_hex("window")
        text = ui_hex("text")
        self.swatch.setStyleSheet(
            "QFrame {"
            f" background: {paper}; color: {ink};"
            f" border: 8px solid {window}; border-radius: 4px;"
            "}"
        )
        self.swatch.setToolTip(f"Sheet {paper}  ink {ink}  window {window}  text {text}")

    def _test_ready(self) -> bool:
        cfg = self._form_settings().with_env()
        if cfg.disabled:
            return False
        if cfg.webhook_url.strip() or cfg.relay_url.strip():
            return True
        return bool(cfg.slack_bot_token.strip() and cfg.slack_channel.strip())

    def _refresh_test(self) -> None:
        cfg = self._form_settings().with_env()
        self.builtin.setVisible(cfg.destination_name() == "PanelCAD team")
        ready = self._test_ready()
        self.test_btn.setEnabled(ready)
        if ready:
            self.test_btn.setToolTip("")
        elif cfg.disabled:
            self.test_btn.setToolTip("Bug reports are turned off.")
        else:
            self.test_btn.setToolTip(TEST_TIP)

    def _toggle_secrets(self, show: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self.webhook.setEchoMode(mode)
        self.token.setEchoMode(mode)

    def _form_settings(self) -> BugSettings:
        stored = load_stored_settings()
        return BugSettings(
            webhook_url=self.webhook.text().strip(),
            slack_bot_token=self.token.text().strip(),
            slack_channel=self.channel.text().strip(),
            relay_url=stored.relay_url,
            disabled=self.reports_off.isChecked(),
        )

    def _save(self) -> None:
        from panelcad.gui import bug_report_dialog as dialogs

        try:
            save_settings(self._form_settings())
            set_autosave_seconds(int(self.autosave.value()))
            self._save_ai()
        except OSError as exc:
            dialogs.QMessageBox.warning(self, "Settings", f"could not save settings: {exc}")
            return
        dialogs.QMessageBox.information(self, "Settings", "Saved settings.")

    def _save_ai(self) -> None:
        from panelcad.ai.config import AiLookupConfig, save_ai_lookup

        save_ai_lookup(
            AiLookupConfig(
                enabled=self.ai_lookup.isChecked(),
                provider=str(self.ai_provider.currentData() or ""),
                endpoint=self.ai_endpoint.text().strip(),
                api_key=self.ai_key.text().strip(),
                model=self.ai_model.text().strip(),
            )
        )

    def _test(self) -> None:
        from panelcad.gui import bug_report_dialog as dialogs

        session = self._session or Session.create("Bug report test")
        try:
            result = session.report_bug(
                "PanelCAD test report",
                "Send test report from Settings.",
                include_project=False,
                include_screenshot=False,
                send=True,
                settings=self._form_settings(),
            )
        except Exception as exc:  # noqa: BLE001
            dialogs.QMessageBox.warning(self, "Bug reports", str(exc))
            return
        ok, text = _send_notice(result)
        if ok:
            dialogs.QMessageBox.information(self, "Bug reports", text)
            return
        dialogs.QMessageBox.warning(self, "Bug reports", text)
