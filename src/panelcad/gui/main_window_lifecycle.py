"""Dirty-state, save-prompt, background worker, update, and bug-report lifecycle for :class:`MainWindow`."""

from __future__ import annotations

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from panelcad import __version__
from panelcad.api.session import Session


class MainWindowLifecycleMixin:
    """Unsaved-change prompts, background workers, update checks, and close handling."""

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
        if hasattr(self, "wiring_walkthrough_panel"):
            self.wiring_walkthrough_panel.set_session(self.session)
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
            # A drag still open would nest into the new session's empty history
            # if we only swapped the Session. Cancel it first; reopen is a
            # clean stack, then new edits record again.
            if hasattr(self, "canvas"):
                self.canvas.drop_open_gesture()
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
