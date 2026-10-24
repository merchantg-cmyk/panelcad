"""Convert drawing standard: target, scope, save-as, and a chunk review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.convert.from_to_check import build_from_to_diff_rows, check_connections
from panelcad.convert.netlist import same_netlist
from panelcad.convert.plan import (
    ConversionDecision,
    decision_original_value,
    decision_proposal_value,
    resolve_conversion_decision,
    sort_decisions_by_confidence,
    valid_decision_options,
)
from panelcad.core.models import Project, Sheet
from panelcad.core.refs import sheet_drawing_standard

THUMB_W = 520
FULL_W = 1280


def _default_out(session: Session, target: str) -> Path:
    token = "iec" if target == "iec" else "nema"
    source = session.path
    if source is not None:
        text = source.name
        if text.endswith(".pcad.yaml"):
            stem = text[: -len(".pcad.yaml")]
            return source.with_name(f"{stem}-{token}.pcad.yaml")
        return source.with_name(f"{source.stem}-{token}{source.suffix or '.yaml'}")
    slug = "".join(ch if ch.isalnum() else "-" for ch in session.project.name).strip("-") or "project"
    return Path.cwd() / f"{slug}-{token}.pcad.yaml"


def _resolve_sheet_in_project(project: Project, sheet_id: str) -> Sheet | None:
    if not sheet_id:
        return project.sheets[0] if project.sheets else None
    try:
        return project.sheet(sheet_id)
    except KeyError:
        cs_block = dict(project.properties.get("convert_standard") or {})
        mp_block = dict(cs_block.get("multipage") or {})
        consol = dict(
            mp_block.get("target_for_sheet")
            or dict(cs_block.get("consolidated") or {}).get("target_for_sheet")
            or {}
        )
        mapped = str(consol.get(sheet_id) or "")
        if not mapped and sheet_id in (mp_block.get("removed_sheets") or []):
            kept = list(mp_block.get("kept_sheets") or [])
            mapped = str(kept[0]) if kept else ""
        if not mapped:
            return None
        try:
            return project.sheet(mapped)
        except KeyError:
            return None


def _changed_component_ids(
    before: Project | None,
    after: Project,
    sheet_id: str,
    *,
    tag_map: dict[str, str] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> tuple[set[str], set[str]]:
    """Return ``(before_highlight_ids, after_highlight_ids)`` for ``sheet_id``."""

    before_ids: set[str] = set()
    after_ids: set[str] = set()
    tmap = dict(tag_map or {})
    inv_tmap = {v: k for k, v in tmap.items()}

    before_sheet = _resolve_sheet_in_project(before, sheet_id) if before is not None else None
    after_sheet = _resolve_sheet_in_project(after, sheet_id)

    before_by_id = {c.id: c for c in (before_sheet.components if before_sheet else [])}
    after_by_id = {c.id: c for c in (after_sheet.components if after_sheet else [])}

    for old_id, comp in before_by_id.items():
        new_id = tmap.get(old_id, old_id)
        after_comp = after_by_id.get(new_id)
        if new_id != old_id or after_comp is None or after_comp.library_id != comp.library_id:
            before_ids.add(old_id)
            if after_comp is not None:
                after_ids.add(new_id)

    for new_id, comp in after_by_id.items():
        old_id = inv_tmap.get(new_id, new_id)
        before_comp = before_by_id.get(old_id)
        if old_id != new_id or before_comp is None or before_comp.library_id != comp.library_id:
            after_ids.add(new_id)
            if before_comp is not None:
                before_ids.add(old_id)

    for dec in decisions or []:
        cid = str(dec.get("component_id") or "")
        if not cid:
            continue
        if cid in before_by_id:
            before_ids.add(cid)
        if cid in after_by_id:
            after_ids.add(cid)
        mapped_new = tmap.get(cid)
        if mapped_new and mapped_new in after_by_id:
            after_ids.add(mapped_new)
        mapped_old = inv_tmap.get(cid)
        if mapped_old and mapped_old in before_by_id:
            before_ids.add(mapped_old)

    return before_ids, after_ids


def sheet_thumbnail(
    project: Project,
    sheet_id: str,
    catalog: object,
    width: int = THUMB_W,
    *,
    highlight_ids: set[str] | list[str] | None = None,
) -> QPixmap | None:
    """Before/after preview with optional changed-component highlights."""

    sheet = _resolve_sheet_in_project(project, sheet_id)
    if sheet is None:
        return None
    try:
        from panelcad.render.page import paint_sheet

        pw, ph = sheet.page.dimensions()
        scale = width / max(pw, 1.0)
        image = QImage(
            max(1, int(pw * scale)),
            max(1, int(ph * scale)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        painter.scale(scale, scale)
        index = next(i for i, page in enumerate(project.sheets) if page.id == sheet.id)
        paint_sheet(
            painter,
            project,
            sheet,
            catalog,  # type: ignore[arg-type]
            sheet_index=index,
            sheet_count=len(project.sheets),
        )
        if highlight_ids:
            hl_set = {str(x) for x in highlight_ids if x}
            pen = QPen(QColor(245, 158, 11, 220), 1.2)
            brush = QBrush(QColor(251, 191, 36, 52))
            painter.setPen(pen)
            painter.setBrush(brush)
            for comp in sheet.components:
                if comp.id not in hl_set:
                    continue
                try:
                    from panelcad.render.wiring import component_obstacle

                    x0, y0, x1, y1 = component_obstacle(sheet, catalog, comp, pad=2.0)  # type: ignore[arg-type]
                    painter.drawRoundedRect(
                        int(round(x0)),
                        int(round(y0)),
                        int(round(max(4.0, x1 - x0))),
                        int(round(max(4.0, y1 - y0))),
                        2,
                        2,
                    )
                except Exception:  # noqa: BLE001
                    painter.drawRoundedRect(
                        int(round(comp.x - 6.0)),
                        int(round(comp.y - 6.0)),
                        12,
                        12,
                        2,
                        2,
                    )
        painter.end()
        return QPixmap.fromImage(image)
    except Exception:  # noqa: BLE001
        return None


class CompareDialog(QDialog):
    """Full-size before and after, From-To diff tab, and interactive Decisions review pane."""

    def __init__(
        self,
        session: Session,
        before: Project | None,
        after: Project,
        sheet_id: str = "",
        parent: QWidget | None = None,
        *,
        workspace: str | Path | None = None,
        result: dict[str, Any] | None = None,
        conversion_result: dict[str, Any] | None = None,
    ) -> None:
        if result is None and conversion_result is not None:
            result = conversion_result
        super().__init__(parent)
        self.session = session
        self.before = before
        self.after = after
        if not sheet_id and after.sheets:
            sheet_id = after.sheets[0].id
        self.sheet_id = sheet_id
        self._workspace = str(workspace or getattr(parent, "_workspace", "") or "")
        self._result = dict(result or getattr(parent, "_last_result", {}) or {})
        self._ws_data = _load_workspace_data(self._workspace)
        if self.before is None and self._ws_data.get("source_project"):
            try:
                self.before = Project.model_validate(self._ws_data["source_project"])
            except Exception:  # noqa: BLE001
                self.before = None

        self.setObjectName("convert-compare")
        self.setModal(False)
        self.setWindowTitle(f"Before and after — {self.sheet_id}")
        self.resize(1680, 960)

        root = QVBoxLayout(self)

        # Top bar: summary bar + highlight toggle + Apply/Save buttons
        top_row = QHBoxLayout()
        self.summary_bar = QLabel()
        self.summary_bar.setObjectName("compare-summary-bar")
        top_row.addWidget(self.summary_bar, 1)

        self.highlight_toggle = QCheckBox("Highlight changed items")
        self.highlight_toggle.setObjectName("compare-highlight-toggle")
        self.highlight_toggle.setChecked(True)
        self.highlight_toggle.toggled.connect(self._refresh_sheet_previews)
        top_row.addWidget(self.highlight_toggle)

        self.apply_decisions_btn = QPushButton("Apply Decisions")
        self.apply_decisions_btn.setObjectName("compare-apply-decisions-btn")
        self.apply_decisions_btn.clicked.connect(self.apply_all_decisions)
        top_row.addWidget(self.apply_decisions_btn)

        self.accept_save_btn = QPushButton("Accept & Save")
        self.accept_save_btn.setObjectName("compare-accept-save-btn")
        self.accept_save_btn.clicked.connect(self.accept_and_save)
        top_row.addWidget(self.accept_save_btn)

        root.addLayout(top_row)

        # Tabs: Sheet Compare | From-To Diff | Decisions Review
        self.tabs = QTabWidget()
        self.tabs.setObjectName("compare-tabs")

        # 1. Sheet Compare tab
        self.sheet_tab = QWidget()
        self.sheet_tab.setObjectName("compare-sheet-tab")
        sheet_row = QHBoxLayout(self.sheet_tab)
        self._before_image_label: QLabel | None = None
        self._after_image_label: QLabel | None = None
        self.highlighted_before_ids: set[str] = set()
        self.highlighted_after_ids: set[str] = set()
        sheet_row.addWidget(self._pane("Before", self.before, self.sheet_id, session))
        sheet_row.addWidget(self._pane("After", self.after, self.sheet_id, session))
        self.tabs.addTab(self.sheet_tab, "Sheet Compare")

        # 2. Side-by-side From-To Diff tab
        self.from_to_tab = QWidget()
        self.from_to_tab.setObjectName("compare-from-to-tab")
        ft_layout = QVBoxLayout(self.from_to_tab)
        self.from_to_table = QTableWidget(0, 5)
        self.from_to_table.setObjectName("compare-from-to-table")
        self.from_to_table.setHorizontalHeaderLabels(
            ["Old Wire #", "Old From -> To", "New Wire #", "New From -> To", "Status"]
        )
        self.from_to_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        ft_layout.addWidget(self.from_to_table)
        self.tabs.addTab(self.from_to_tab, "From-To Diff")

        # 3. Interactive Decisions Review tab
        self.decisions_tab = QWidget()
        self.decisions_tab.setObjectName("compare-decisions-tab")
        dec_layout = QVBoxLayout(self.decisions_tab)
        self.decisions_table = QTableWidget(0, 8)
        self.decisions_table.setObjectName("compare-decisions-table")
        self.decisions_table.setHorizontalHeaderLabels(
            [
                "Component / Item",
                "Sheet",
                "Reason",
                "Confidence",
                "Original",
                "Proposal",
                "Action",
                "Valid Option",
            ]
        )
        self.decisions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        dec_layout.addWidget(self.decisions_table)
        self.tabs.addTab(self.decisions_tab, "Decisions Review")

        root.addWidget(self.tabs, 1)

        self._sorted_decisions: list[dict[str, Any]] = []
        self.refresh_all()

    def _pane(
        self,
        caption: str,
        project: Project | None,
        sheet_id: str,
        session: Session,
    ) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        title = QLabel(caption)
        title.setObjectName(f"compare:{caption}")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        image = QLabel("no preview")
        image.setObjectName(f"compare-image:{caption}")
        image.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        if caption == "Before":
            self._before_image_label = image
        else:
            self._after_image_label = image
        scroll.setWidget(image)
        layout.addWidget(scroll, 1)
        return wrap

    def _current_mapping(self) -> tuple[dict[str, str], Any, dict[str, dict[str, Any]]]:
        ws = _load_workspace_data(self._workspace) or self._ws_data
        if ws:
            self._ws_data = ws
        pub: dict[str, Any] = {}
        if ws and "chunks" in ws:
            try:
                from panelcad.convert.engine import _public
                pub = _public(ws)
            except Exception:  # noqa: BLE001
                pub = {}
        cs_props = dict(self.after.properties.get("convert_standard") or {})
        tag_map = dict(
            ws.get("tag_map")
            or pub.get("tag_map")
            or self._result.get("tag_map")
            or cs_props.get("tags")
            or cs_props.get("tag_map")
            or {}
        )
        pin_rewrites = (
            ws.get("pin_rewrites")
            or pub.get("pin_rewrites")
            or self._result.get("pin_rewrites")
            or []
        )
        rail_map = dict(
            ws.get("rail_map")
            or pub.get("rail_map")
            or self._result.get("rail_map")
            or cs_props.get("rails")
            or {}
        )
        return tag_map, pin_rewrites, rail_map

    def _current_decisions(self) -> list[dict[str, Any]]:
        ws = _load_workspace_data(self._workspace) or self._ws_data
        if ws:
            self._ws_data = ws
        pub_decs: list[Any] = []
        if ws and "chunks" in ws:
            try:
                from panelcad.convert.engine import _public
                pub_decs = list(_public(ws).get("decisions") or [])
            except Exception:  # noqa: BLE001
                pub_decs = []
        raw = list(ws.get("decisions") or []) + list(pub_decs) + list(self._result.get("decisions") or []) + list(self.after.properties.get("last_conversion_decisions") or [])
        out: list[dict[str, Any]] = []
        key_to_idx: dict[tuple[str, str, str], int] = {}
        ws_explicit = list(ws.get("decisions") or [])
        raw = list(reversed(ws_explicit)) + list(pub_decs) + list(self._result.get("decisions") or []) + list(self.after.properties.get("last_conversion_decisions") or [])
        for item in raw:
            d_dict = item.to_dict() if isinstance(item, ConversionDecision) else (dict(item) if isinstance(item, dict) else {})
            if not d_dict:
                continue
            key = (
                str(d_dict.get("reason_code") or d_dict.get("flag") or ""),
                str(d_dict.get("component_id") or ""),
                str(d_dict.get("sheet_id") or ""),
            )
            if key not in key_to_idx:
                key_to_idx[key] = len(out)
                out.append(d_dict)

        # Also include deterministic tag-rename / symbol-swap decisions (confidence=1.0, needs_review=False)
        # so the reviewer can inspect or override any converted item in CompareDialog.
        tag_map, _pin_rewrites, _rail_map = self._current_mapping()
        covered_comps = {
            str(d.get("component_id") or "")
            for d in out
        } | {
            str((d.get("before") or {}).get("id") or (d.get("before") or {}).get("tag") or "")
            for d in out
        }
        sheet_by_comp: dict[str, str] = {}
        if self.before is not None:
            for sh in self.before.sheets:
                for c in sh.components:
                    sheet_by_comp[c.id] = sh.id
        for old_tag, new_tag in sorted(tag_map.items()):
            if not old_tag or not new_tag or old_tag == new_tag:
                continue
            if old_tag in covered_comps or new_tag in covered_comps:
                continue
            sh_id = sheet_by_comp.get(old_tag, self.sheet_id)
            out.append(
                ConversionDecision(
                    reason_code="ambiguous_role",
                    component_id=new_tag,
                    sheet_id=sh_id,
                    before={"id": old_tag, "tag": old_tag},
                    after={"id": new_tag, "tag": new_tag},
                    proposal={"id": new_tag, "valid": True, "accepted": True},
                    confidence=1.0,
                    needs_review=False,
                    message=f"Renamed {old_tag} -> {new_tag}",
                    chunk_id="tags",
                ).to_dict()
            )
        return out

    def _target_standard(self) -> str:
        ws = self._ws_data or {}
        target = str(
            ws.get("target")
            or self._result.get("target")
            or (self.after.properties.get("convert_standard") or {}).get("target")
            or sheet_drawing_standard(self.after, self.after.sheets[0] if self.after.sheets else None)
            or "NEMA/ANSI"
        )
        return target

    def summary_counts(self) -> dict[str, Any]:
        """Compute swapped, renamed, needs_review, netlist_ok, and from_to_ok counts."""

        tag_map, pin_rewrites, rail_map = self._current_mapping()
        decisions = self._current_decisions()

        # Swapped count: components whose library_id changed between before and after
        swapped_ids: set[str] = set()
        if self.before is not None:
            for b_sheet in self.before.sheets:
                a_sheet = _resolve_sheet_in_project(self.after, b_sheet.id)
                if a_sheet is None:
                    continue
                a_by_id = {c.id: c for c in a_sheet.components}
                for b_comp in b_sheet.components:
                    mapped_id = tag_map.get(b_comp.id, b_comp.id)
                    a_comp = a_by_id.get(mapped_id)
                    if a_comp is not None and a_comp.library_id != b_comp.library_id:
                        swapped_ids.add(mapped_id)
        for d in decisions:
            if str(d.get("reason_code") or "") == "symbol_swap":
                b_lib = (d.get("before") or {}).get("library_id")
                a_lib = (d.get("after") or {}).get("library_id")
                if b_lib and a_lib and b_lib != a_lib:
                    swapped_ids.add(str(d.get("component_id") or ""))

        # Renamed count: tags in tag_map where old != new (plus any role/tag changes applied)
        renamed_pairs = {k: v for k, v in tag_map.items() if k and v and k != v}
        n_renamed = len(renamed_pairs)

        # Needs review count: decisions still marked needs_review
        n_needs_review = sum(1 for d in decisions if bool(d.get("needs_review", False)))

        # Netlist & From-To status
        if self.before is not None:
            try:
                from panelcad.convert.netlist import sheet_edges
                netlist_ok = same_netlist(
                    sheet_edges(self.before),
                    sheet_edges(self.after),
                    tag_map=tag_map,
                    pin_rewrites=pin_rewrites,
                    rail_map=rail_map,
                )
            except Exception:  # noqa: BLE001
                netlist_ok = bool(self._result.get("same_netlist", True))
            try:
                ft_report = check_connections(
                    self.before,
                    self.after,
                    self.session.catalog,
                    tag_map=tag_map,
                    pin_rewrites=pin_rewrites,
                    rail_map=rail_map,
                )
                from_to_ok = bool(ft_report.get("ok", True))
            except Exception:  # noqa: BLE001
                from_to_ok = bool((self._result.get("connection_check") or self._result.get("from_to_check") or {}).get("ok", True))
        else:
            netlist_ok = bool(self._result.get("same_netlist", True))
            from_to_ok = bool((self._result.get("from_to_check") or {}).get("ok", True))

        return {
            "swapped": len(swapped_ids),
            "renamed": n_renamed,
            "needs_review": n_needs_review,
            "netlist_ok": netlist_ok,
            "from_to_ok": from_to_ok,
        }

    def _refresh_summary_bar(self) -> None:
        counts = self.summary_counts()
        net_str = "OK" if counts["netlist_ok"] else "FAIL"
        ft_str = "OK" if counts["from_to_ok"] else "FAIL"
        self.summary_bar.setText(
            f"{counts['swapped']} swapped, {counts['renamed']} renamed, "
            f"{counts['needs_review']} needs review, netlist: {net_str}, from-to: {ft_str}"
        )

    def _refresh_sheet_previews(self) -> None:
        tag_map, _pin_rewrites, _rail_map = self._current_mapping()
        decisions = self._current_decisions()
        b_hl, a_hl = _changed_component_ids(
            self.before,
            self.after,
            self.sheet_id,
            tag_map=tag_map,
            decisions=decisions,
        )
        self.highlighted_before_ids = b_hl
        self.highlighted_after_ids = a_hl
        use_hl = self.highlight_toggle.isChecked()

        if self._before_image_label is not None and self.before is not None:
            pix = sheet_thumbnail(
                self.before,
                self.sheet_id,
                self.session.catalog,
                width=FULL_W,
                highlight_ids=b_hl if use_hl else None,
            )
            if pix is not None:
                self._before_image_label.setPixmap(pix)
                self._before_image_label.setText("")
                self._before_image_label.adjustSize()

        if self._after_image_label is not None and self.after is not None:
            pix = sheet_thumbnail(
                self.after,
                self.sheet_id,
                self.session.catalog,
                width=FULL_W,
                highlight_ids=a_hl if use_hl else None,
            )
            if pix is not None:
                self._after_image_label.setPixmap(pix)
                self._after_image_label.setText("")
                self._after_image_label.adjustSize()

    def _refresh_from_to_table(self) -> None:
        tag_map, pin_rewrites, rail_map = self._current_mapping()
        rows: list[dict[str, str]] = []
        if self.before is not None:
            try:
                rows = build_from_to_diff_rows(
                    self.before,
                    self.after,
                    self.session.catalog,
                    tag_map=tag_map,
                    pin_rewrites=pin_rewrites,
                    rail_map=rail_map,
                )
            except Exception:  # noqa: BLE001
                rows = []
        if not rows:
            ft_check = dict(self._result.get("from_to_check") or self._ws_data.get("from_to_check") or {})
            rows = list(ft_check.get("from_to_diff") or [])

        self.from_to_table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            status = str(row.get("status") or "MATCH")
            vals = [
                str(row.get("old_wire_number") or ""),
                str(row.get("old_from_to") or ""),
                str(row.get("new_wire_number") or ""),
                str(row.get("new_from_to") or ""),
                status,
            ]
            for c_idx, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if status == "MISMATCH":
                    item.setBackground(QColor(254, 226, 226))
                elif status == "RENAMED":
                    item.setBackground(QColor(254, 243, 199))
                elif status == "LOCKED":
                    item.setBackground(QColor(229, 231, 235))
                else:
                    item.setBackground(QColor(220, 252, 231))
                self.from_to_table.setItem(r_idx, c_idx, item)

    def _refresh_decisions_table(self) -> None:
        raw_decisions = self._current_decisions()
        sorted_decs = sort_decisions_by_confidence(raw_decisions)
        self._sorted_decisions = [
            d.to_dict() if isinstance(d, ConversionDecision) else dict(d)
            for d in sorted_decs
        ]
        target_std = self._target_standard()

        self.decisions_table.setRowCount(len(self._sorted_decisions))
        for r_idx, dec in enumerate(self._sorted_decisions):
            comp_id = str(dec.get("component_id") or "")
            sheet_id = str(dec.get("sheet_id") or "")
            reason = str(dec.get("reason_code") or dec.get("flag") or "")
            conf = float(dec.get("confidence", 1.0))
            orig_val = decision_original_value(dec)
            prop_val = decision_proposal_value(dec)

            c0 = QTableWidgetItem(comp_id)
            c0.setData(Qt.ItemDataRole.UserRole, dec)
            c1 = QTableWidgetItem(sheet_id)
            c2 = QTableWidgetItem(reason)
            c3 = QTableWidgetItem(f"{conf:.2f}")
            c3.setData(Qt.ItemDataRole.UserRole, conf)
            c4 = QTableWidgetItem(orig_val)
            c5 = QTableWidgetItem(prop_val)

            for c_idx, item in enumerate((c0, c1, c2, c3, c4, c5)):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bool(dec.get("needs_review", False)):
                    item.setBackground(QColor(254, 243, 199))
                self.decisions_table.setItem(r_idx, c_idx, item)

            action_combo = QComboBox()
            action_combo.setObjectName(f"decision-action:{r_idx}")
            action_combo.addItem("Accept", "accept")
            action_combo.addItem("Change", "change")
            action_combo.addItem("Keep original", "keep_original")

            cur_action = str(dec.get("action") or "")
            if cur_action == "change":
                action_combo.setCurrentIndex(1)
            elif cur_action == "keep_original":
                action_combo.setCurrentIndex(2)
            else:
                action_combo.setCurrentIndex(0)

            option_combo = QComboBox()
            option_combo.setObjectName(f"decision-option:{r_idx}")
            valid_opts = valid_decision_options(
                dec,
                target=target_std,
                project=self.after,
                catalog=self.session.catalog,
            )
            for opt in valid_opts:
                option_combo.addItem(str(opt), str(opt))
            chosen = str(dec.get("chosen_option") or "")
            if chosen:
                idx = option_combo.findData(chosen)
                if idx >= 0:
                    option_combo.setCurrentIndex(idx)

            self.decisions_table.setCellWidget(r_idx, 6, action_combo)
            self.decisions_table.setCellWidget(r_idx, 7, option_combo)

    def refresh_all(self) -> None:
        self._ws_data = _load_workspace_data(self._workspace)
        self._refresh_sheet_previews()
        self._refresh_from_to_table()
        self._refresh_decisions_table()
        self._refresh_summary_bar()

    def resolve_decision(
        self,
        row_or_decision: int | str | dict[str, Any] | ConversionDecision,
        action: str | None = None,
        chosen_option: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a single decision from the Decisions Review table and refresh."""

        target_dec: int | str | dict[str, Any] | ConversionDecision = row_or_decision
        if isinstance(row_or_decision, int):
            r_idx = row_or_decision
            if 0 <= r_idx < len(self._sorted_decisions):
                target_dec = self._sorted_decisions[r_idx]
            act_widget = self.decisions_table.cellWidget(r_idx, 6)
            opt_widget = self.decisions_table.cellWidget(r_idx, 7)
            if action is None and isinstance(act_widget, QComboBox):
                action = str(act_widget.currentData() or act_widget.currentText())
            if chosen_option is None and isinstance(opt_widget, QComboBox):
                chosen_option = str(opt_widget.currentData() or opt_widget.currentText())

        act = str(action or "accept")
        if self.after is not None and self.session.project is not self.after:
            self.session.project = self.after
        res = resolve_conversion_decision(
            self.session,
            target_dec,
            act,
            chosen_option=chosen_option,
            project=self.after,
            workspace=self._workspace or None,
            catalog=self.session.catalog,
            source_project=self.before,
        )
        self.after = self.session.project
        self._result["decisions"] = res.get("decisions", [])
        self._result["same_netlist"] = res.get("same_netlist", True)
        self._result["from_to_check"] = res.get("from_to_check", {})
        self.refresh_all()
        parent = self.parent()
        if isinstance(parent, ConvertDialog) and parent._workspace:
            parent.show_review(self.session.convert_standard_review(parent._workspace))
        return res

    def apply_all_decisions(self) -> list[dict[str, Any]]:
        """Apply every row in ``decisions_table`` whose action combo was set or modified."""

        results: list[dict[str, Any]] = []
        snapshot = list(self._sorted_decisions)
        for r_idx, dec in enumerate(snapshot):
            act_widget = self.decisions_table.cellWidget(r_idx, 6)
            opt_widget = self.decisions_table.cellWidget(r_idx, 7)
            act = (
                str(act_widget.currentData() or act_widget.currentText())
                if isinstance(act_widget, QComboBox)
                else "accept"
            )
            opt = (
                str(opt_widget.currentData() or opt_widget.currentText())
                if isinstance(opt_widget, QComboBox)
                else None
            )
            res = resolve_conversion_decision(
                self.session,
                dec,
                act,
                chosen_option=opt,
                workspace=self._workspace or None,
                catalog=self.session.catalog,
                source_project=self.before,
            )
            results.append(res)
        self.after = self.session.project
        self.refresh_all()
        parent = self.parent()
        if isinstance(parent, ConvertDialog) and parent._workspace:
            parent.show_review(self.session.convert_standard_review(parent._workspace))
        return results

    _apply_single_decision_row = resolve_decision

    def accept_and_save(self, out_path: str | Path | None = None) -> Path | None:
        """Write the converted save-as copy while keeping any unresolved decisions in Problems."""

        target_out = out_path
        if target_out is None:
            ws = _load_workspace_data(self._workspace)
            target_out = ws.get("out_path") or self._result.get("out")
            if not target_out:
                parent = self.parent()
                if isinstance(parent, ConvertDialog) and parent.path.text().strip():
                    target_out = parent.path.text().strip()
        if target_out:
            p = Path(str(target_out))
            if self.after is not None and self.session.project is not self.after:
                self.session.project = self.after
            self.session.save(p)
            return p
        return None

    _on_accept_save = accept_and_save


class _SheetThumb(QFrame):
    """Click opens the full-size before/after view."""

    def __init__(
        self,
        owner: "ConvertDialog",
        caption: str,
        project: Project | None,
        sheet_id: str,
        *,
        highlight_ids: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._owner = owner
        self._sheet_id = sheet_id
        self.setObjectName(f"thumb:{caption}:{sheet_id}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to open both sheets full size.")
        layout = QVBoxLayout(self)
        caption_label = QLabel(caption)
        image = QLabel("no preview")
        image.setMinimumSize(THUMB_W, 280)
        if project is not None:
            pix = sheet_thumbnail(
                project,
                sheet_id,
                owner.session.catalog,
                highlight_ids=highlight_ids,
            )
            if pix is not None:
                image.setPixmap(pix)
                image.setText("")
        for child in (caption_label, image):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(caption_label)
        layout.addWidget(image)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._sheet_id:
            self._owner.open_compare(self._sheet_id)
        super().mouseReleaseEvent(event)


class ConvertDialog(QDialog):
    """Menu command 'Convert drawing standard...'."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Convert drawing standard")
        self.resize(1480, 900)
        self._workspace = ""
        self._source: Project | None = None
        self._compare: CompareDialog | None = None
        self._imported_draft = None
        self._last_result: dict[str, Any] = {}
        self._syncing_tag_mode = False

        current = sheet_drawing_standard(session.project, session.sheet() if session.project.sheets else None)
        self.iec = QRadioButton("IEC")
        self.nema = QRadioButton("NEMA/ANSI")
        (self.nema if current == "IEC" else self.iec).setChecked(True)
        standards = QButtonGroup(self)
        standards.addButton(self.iec)
        standards.addButton(self.nema)
        self.iec.toggled.connect(self._refresh_path)
        self.nema.toggled.connect(self._refresh_path)

        self.whole = QRadioButton("Whole project")
        self.picked = QRadioButton("Selected sheets")
        self.whole.setChecked(True)
        scope = QButtonGroup(self)
        scope.addButton(self.whole)
        scope.addButton(self.picked)

        self.keep_numbers = QCheckBox("Keep existing numbers")
        self.keep_numbers.setObjectName("keep-existing-numbers")
        self.keep_numbers.setToolTip(
            "Leave each device's number as it is. "
            "Unchecked, each letter family is numbered 1, 2, 3 in sheet order."
        )

        self.tag_mode_combo = QComboBox()
        self.tag_mode_combo.setObjectName("convert-tag-mode-combo")
        self.tag_mode_combo.addItem("Sequential", "sequential")
        self.tag_mode_combo.addItem("Keep numbers", "keep_numbers")
        self.tag_mode_combo.setToolTip(
            "Sequential numbers each device family 1, 2, 3 in sheet order. "
            "Keep numbers preserves existing device numbers."
        )
        self.keep_numbers.toggled.connect(self._on_keep_numbers_toggled)
        self.tag_mode_combo.currentIndexChanged.connect(self._on_tag_mode_combo_changed)

        self.wire_mode_combo = QComboBox()
        self.wire_mode_combo.setObjectName("convert-wire-mode-combo")
        self.wire_mode_combo.addItem("NEMA line-reference", "line_ref")
        self.wire_mode_combo.addItem("Keep existing wire numbers", "keep")
        self.wire_mode_combo.setToolTip(
            "NEMA line-reference converts IEC wire numbers to rung-derived numbers (101, 102, ...). "
            "Keep existing wire numbers preserves the original wire numbers unchanged."
        )

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("convert-mode-combo")
        self.mode_combo.addItem("Standard (rules only)", "rules_only")
        self.mode_combo.addItem("AI-assisted", "ai_assisted")
        self.mode_combo.setToolTip(
            "Standard uses deterministic rules only. "
            "AI-assisted adds validated advisory suggestions for ambiguous roles, unmapped symbols, notes, and layout."
        )

        self.sheets = QListWidget()
        active = session.active_sheet_id
        for sheet in session.project.sheets:
            item = QListWidgetItem(f"{sheet.id}   {sheet.title}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sheet.id == active else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, sheet.id)
            self.sheets.addItem(item)

        self.path = QLineEdit()
        self._refresh_path()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)

        self.import_scan_btn = QPushButton("Import IEC PDF / scan...")
        self.import_scan_btn.setObjectName("convert-import-scan-btn")
        self.import_scan_btn.setToolTip(
            "Import an IEC PDF or raster scan into a draft From-To wire table for review before converting."
        )
        self.import_scan_btn.clicked.connect(self._on_import_scan)

        self.import_from_to_table = QTableWidget(0, 6)
        self.import_from_to_table.setObjectName("convert-import-from-to-table")
        self.import_from_to_table.setHorizontalHeaderLabels(
            ["Wire #", "From Tag", "From Pin", "To Tag", "To Pin", "Confidence"]
        )
        self.import_from_to_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.import_from_to_table.setMinimumHeight(140)
        self.import_from_to_table.hide()

        convert = QPushButton("Convert")
        convert.clicked.connect(self._on_convert)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        form = QVBoxLayout()
        form.addWidget(QLabel("Target standard"))
        row = QHBoxLayout()
        row.addWidget(self.iec)
        row.addWidget(self.nema)
        row.addStretch(1)
        form.addLayout(row)
        form.addWidget(QLabel("Scope"))
        row = QHBoxLayout()
        row.addWidget(self.whole)
        row.addWidget(self.picked)
        row.addStretch(1)
        form.addLayout(row)
        form.addWidget(QLabel("Tag numbering"))
        form.addWidget(self.tag_mode_combo)
        form.addWidget(self.keep_numbers)
        form.addWidget(QLabel("Wire numbering"))
        form.addWidget(self.wire_mode_combo)
        form.addWidget(QLabel("Conversion type"))
        form.addWidget(self.mode_combo)
        form.addWidget(self.import_scan_btn)
        form.addWidget(self.import_from_to_table)
        form.addWidget(self.sheets)
        form.addWidget(QLabel("Save as"))
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(convert)
        row.addWidget(close)
        row.addStretch(1)
        form.addLayout(row)

        self.review_host = QWidget()
        self.review_layout = QVBoxLayout(self.review_host)
        self.review_layout.setContentsMargins(8, 8, 8, 8)
        self.review_layout.addWidget(QLabel("Review appears after Convert."))
        self.review_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.review_host)
        scroll.setObjectName("convert-review")
        scroll.setMinimumHeight(360)

        root = QHBoxLayout(self)
        left = QWidget()
        left.setMinimumWidth(320)
        left.setLayout(form)
        root.addWidget(left, 0)
        root.addWidget(scroll, 1)

    def _on_keep_numbers_toggled(self, checked: bool) -> None:
        if self._syncing_tag_mode:
            return
        self._syncing_tag_mode = True
        try:
            target_data = "keep_numbers" if checked else "sequential"
            idx = self.tag_mode_combo.findData(target_data)
            if idx >= 0:
                self.tag_mode_combo.setCurrentIndex(idx)
        finally:
            self._syncing_tag_mode = False

    def _on_tag_mode_combo_changed(self, _index: int) -> None:
        if self._syncing_tag_mode:
            return
        self._syncing_tag_mode = True
        try:
            is_keep = self.tag_mode_combo.currentData() == "keep_numbers"
            self.keep_numbers.setChecked(is_keep)
        finally:
            self._syncing_tag_mode = False

    def _target(self) -> str:
        return "iec" if self.iec.isChecked() else "nema"

    def _refresh_path(self) -> None:
        if self.path.text().strip() and getattr(self, "_path_edited", False):
            return
        self.path.setText(str(_default_out(self.session, self._target())))

    def _browse(self) -> None:
        name, _filter = QFileDialog.getSaveFileName(
            self,
            "Save converted project",
            self.path.text(),
            "PanelCAD (*.pcad.yaml *.yaml)",
        )
        if name:
            self._path_edited = True
            self.path.setText(name)

    def chosen_sheets(self) -> list[str] | None:
        if self.whole.isChecked():
            return None
        picked: list[str] = []
        for index in range(self.sheets.count()):
            item = self.sheets.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                picked.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return picked

    def _on_convert(self) -> None:
        try:
            self.perform()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))

    def conversion_mode(self) -> str:
        return str(self.mode_combo.currentData() or "rules_only")

    def set_conversion_mode(self, mode: str) -> None:
        idx = self.mode_combo.findData(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)

    def tag_mode(self) -> str:
        if self.keep_numbers.isChecked() or self.tag_mode_combo.currentData() == "keep_numbers":
            return "keep_numbers"
        return "sequential"

    def set_tag_mode(self, mode: str) -> None:
        idx = self.tag_mode_combo.findData(mode)
        if idx >= 0:
            self.tag_mode_combo.setCurrentIndex(idx)
        else:
            self.keep_numbers.setChecked(mode in {"keep_numbers", "keep"})

    def wire_mode(self) -> str:
        return str(self.wire_mode_combo.currentData() or "line_ref")

    def set_wire_mode(self, mode: str) -> None:
        idx = self.wire_mode_combo.findData(mode)
        if idx >= 0:
            self.wire_mode_combo.setCurrentIndex(idx)

    def _on_import_scan(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(
            self,
            "Import IEC PDF or scan",
            "",
            "IEC Prints (*.pdf *.png *.jpg *.jpeg *.json *.yaml *.pcad.yaml)",
        )
        if name:
            try:
                self.load_imported_print(name)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Import IEC PDF / scan", str(exc))

    def load_imported_print(
        self,
        source: object,
        *,
        vision_backend: object = None,
        confidence_threshold: float = 0.80,
    ) -> object:
        """Extract a draft From-To list from an IEC PDF/scan and populate the review table."""

        from panelcad.convert.import_convert import import_iec_print_to_from_to

        draft = import_iec_print_to_from_to(
            source,  # type: ignore[arg-type]
            vision_backend=vision_backend,
            confidence_threshold=confidence_threshold,
            catalog=self.session.catalog,
        )
        self._imported_draft = draft
        self._populate_import_table(draft)
        return draft

    def _populate_import_table(self, draft: object) -> None:
        rows = list(getattr(draft, "from_to_rows", []) or [])
        self.import_from_to_table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            vals = [
                str(row.wire_number or ""),
                str(row.from_tag or ""),
                str(row.from_pin or ""),
                str(row.to_tag or ""),
                str(row.to_pin or ""),
                f"{float(row.confidence):.2f}" + (" (review)" if row.needs_review else ""),
            ]
            for c_idx, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if c_idx == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(row.row_id or f"row_{r_idx + 1}"))
                if c_idx == 5:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.import_from_to_table.setItem(r_idx, c_idx, item)
        self.import_from_to_table.setVisible(True)

    def imported_from_to_rows(self) -> list[dict[str, str]]:
        """Return the current From-To rows from the editable import table."""

        out: list[dict[str, str]] = []
        for r_idx in range(self.import_from_to_table.rowCount()):
            w_item = self.import_from_to_table.item(r_idx, 0)
            ft_item = self.import_from_to_table.item(r_idx, 1)
            fp_item = self.import_from_to_table.item(r_idx, 2)
            tt_item = self.import_from_to_table.item(r_idx, 3)
            tp_item = self.import_from_to_table.item(r_idx, 4)
            row_id = str(w_item.data(Qt.ItemDataRole.UserRole) or f"row_{r_idx + 1}") if w_item else f"row_{r_idx + 1}"
            out.append({
                "row_id": row_id,
                "wire_number": w_item.text().strip() if w_item else "",
                "from_tag": ft_item.text().strip() if ft_item else "",
                "from_pin": fp_item.text().strip() if fp_item else "",
                "to_tag": tt_item.text().strip() if tt_item else "",
                "to_pin": tp_item.text().strip() if tp_item else "",
            })
        return out

    def apply_table_corrections(self) -> object:
        """Apply any edits made in ``import_from_to_table`` back onto the imported From-To draft."""

        if self._imported_draft is None:
            return None
        from panelcad.convert.import_convert import apply_from_to_corrections

        orig_by_id = {
            str(r.row_id or f"row_{idx + 1}"): r
            for idx, r in enumerate(self._imported_draft.from_to_rows)
        }
        edits: list[dict[str, str]] = []
        for row_dict in self.imported_from_to_rows():
            rid = row_dict["row_id"]
            orig = orig_by_id.get(rid)
            if orig is None:
                continue
            if (
                orig.wire_number != row_dict["wire_number"]
                or orig.from_tag != row_dict["from_tag"].lstrip("-").strip()
                or orig.from_pin != row_dict["from_pin"]
                or orig.to_tag != row_dict["to_tag"].lstrip("-").strip()
                or orig.to_pin != row_dict["to_pin"]
            ):
                edits.append(row_dict)
        if edits:
            self._imported_draft = apply_from_to_corrections(self._imported_draft, edits=edits)
            self._populate_import_table(self._imported_draft)
        return self._imported_draft

    def perform(self) -> dict:
        """Run the conversion and fill the review. Tests call this without exec."""

        keep_nums = self.tag_mode() == "keep_numbers"
        w_mode = self.wire_mode()
        if self._imported_draft is not None:
            self.apply_table_corrections()
            result = self.session.import_and_convert_standard(
                self._imported_draft,
                target=self._target(),
                out=self.path.text().strip() or None,
                keep_numbers=keep_nums,
                wire_mode=w_mode,
                mode=self.conversion_mode(),
            )
            self._last_result = dict(result)
            self.show_review(result)
            self._reload_parent()
            return result

        result = self.session.convert_standard(
            self._target(),
            sheets=self.chosen_sheets(),
            out=self.path.text().strip() or None,
            keep_numbers=keep_nums,
            wire_mode=w_mode,
            mode=self.conversion_mode(),
        )
        self._last_result = dict(result)
        self.show_review(result)
        return result

    def show_review(self, result: dict) -> None:
        self._last_result = dict(result)
        self._workspace = str(result.get("workspace") or self._workspace)
        self._source = _load_source(self._workspace)
        while self.review_layout.count():
            item = self.review_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # deleteLater leaves the old label on screen until the event
                # loop runs, so it painted on top of the new header.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        decisions = list(result.get("decisions") or [])
        header = QLabel(
            f"Target {result.get('target')}    pending {len(result.get('pending') or [])}    decisions: {len(decisions)}"
        )
        header.setObjectName("convert-review-header")
        self.review_layout.addWidget(header)
        for chunk in result.get("chunks") or []:
            self.review_layout.addWidget(self._chunk_row(chunk))
        self.review_layout.addStretch(1)
        self.review_layout.activate()
        self.review_host.adjustSize()

    def _chunk_row(self, chunk: dict) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setObjectName(f"chunk:{chunk['id']}")
        layout = QVBoxLayout(frame)
        flags = ", ".join(issue.get("flag", "") for issue in (chunk.get("issues") or [])) or "none"
        reasons = ", ".join(str(r) for r in (chunk.get("reason_codes") or [])) or flags
        summary_lbl = QLabel(
            f"{chunk['id']}    {chunk['status']}    flags: {flags}    reasons: {reasons}"
        )
        summary_lbl.setObjectName(f"chunk-summary:{chunk['id']}")
        layout.addWidget(summary_lbl)
        chunk_decs = list(chunk.get("decisions") or [])
        if chunk_decs:
            dec_lines = [
                f"• [{d.get('reason_code') or d.get('flag')}] {d.get('message') or d.get('detail')}"
                for d in chunk_decs
            ]
            dec_lbl = QLabel("\n".join(dec_lines))
            dec_lbl.setObjectName(f"chunk-decisions:{chunk['id']}")
            dec_lbl.setWordWrap(True)
            layout.addWidget(dec_lbl)
        sheet_id = str(chunk.get("sheet") or "")
        if sheet_id:
            ws_data = _load_workspace_data(self._workspace)
            b_hl, a_hl = _changed_component_ids(
                self._source,
                self.session.project,
                sheet_id,
                tag_map=ws_data.get("tag_map"),
                decisions=ws_data.get("decisions"),
            )
            pics = QHBoxLayout()
            pics.addWidget(self._thumb("Before", self._source, sheet_id, highlight_ids=b_hl))
            pics.addWidget(self._thumb("After", self.session.project, sheet_id, highlight_ids=a_hl))
            layout.addLayout(pics)
            hint = QLabel("Click a picture to open both sheets full size.")
            layout.addWidget(hint)
        suggestion = str(chunk.get("suggestion") or "")
        if suggestion:
            note = QLabel(suggestion)
            note.setWordWrap(True)
            layout.addWidget(note)
        buttons = QHBoxLayout()
        accept = QPushButton("Accept")
        accept.setObjectName(f"accept:{chunk['id']}")
        rerun = QPushButton("Re-run")
        rerun.setObjectName(f"rerun:{chunk['id']}")
        revert = QPushButton("Revert")
        revert.setObjectName(f"revert:{chunk['id']}")
        cid = str(chunk["id"])
        accept.clicked.connect(lambda _checked=False, name=cid: self._accept(name))
        rerun.clicked.connect(lambda _checked=False, name=cid: self._rerun(name))
        revert.clicked.connect(lambda _checked=False, name=cid: self._revert(name))
        buttons.addWidget(accept)
        buttons.addWidget(rerun)
        buttons.addWidget(revert)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return frame

    def _thumb(
        self,
        caption: str,
        project: Project | None,
        sheet_id: str,
        *,
        highlight_ids: set[str] | None = None,
    ) -> QWidget:
        return _SheetThumb(self, caption, project, sheet_id, highlight_ids=highlight_ids)

    def open_compare(self, sheet_id: str) -> CompareDialog:
        previous = self._compare
        if previous is not None:
            previous.close()
        view = CompareDialog(
            self.session,
            self._source,
            self.session.project,
            sheet_id,
            self,
            workspace=self._workspace or None,
            result=self._last_result,
        )
        view.show()
        self._compare = view
        return view

    def _accept(self, chunk_id: str) -> None:
        self.show_review(self.session.convert_standard_accept(chunk_id, self._workspace or None))

    def _rerun(self, chunk_id: str) -> None:
        try:
            result = self.session.convert_standard_rerun(chunk_id, self._workspace or None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))
            return
        self.show_review(result)
        self._reload_parent()

    def _revert(self, chunk_id: str) -> None:
        try:
            result = self.session.convert_standard_revert(chunk_id, self._workspace or None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))
            return
        self.show_review(result)
        self._reload_parent()

    def _reload_parent(self) -> None:
        parent = self.parent()
        canvas = getattr(parent, "canvas", None)
        if canvas is not None:
            canvas.reload()


def _load_workspace_data(workspace: str) -> dict[str, Any]:
    if not workspace:
        return {}
    path = Path(workspace)
    if not path.is_file():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return {}


def _load_source(workspace: str) -> Project | None:
    data = _load_workspace_data(workspace)
    if not data or "source_project" not in data:
        return None
    try:
        return Project.model_validate(data["source_project"])
    except Exception:  # noqa: BLE001
        return None
