"""Control power budget. Pick a supply and read the loads on it."""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session

COLUMNS = ("ref", "sheet", "kind", "steady", "inrush", "source")
HEADERS = ("Ref", "Sheet", "Kind", "Steady", "Inrush", "Source")


def _inrush_label(load: dict) -> str:
    if load.get("inrush_published") and load.get("inrush_va"):
        return f"{load.get('inrush_va')} VA"
    return str(load.get("inrush_note") or "not published")


class SummaryText(QTextBrowser):
    """Read-only summary. The size hint stays small so a long report scrolls."""

    _HINT_H = 128
    _MIN_H = 72

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumHeight(self._MIN_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        self.setObjectName("powerSummary")
        self.apply_theme()
        self.document().setDocumentMargin(4)
        self.document().setDefaultStyleSheet(
            "p { margin: 0 0 3px 0; } ul { margin: 0 0 4px 0; padding-left: 18px; } li { margin: 0; }"
        )

    def apply_theme(self) -> None:
        from PySide6.QtGui import QColor, QPalette

        from panelcad.theme import ui_hex

        self.setStyleSheet(
            "#powerSummary {"
            f" background: {ui_hex('panel')}; color: {ui_hex('text')};"
            f" border: 1px solid {ui_hex('border')};"
            " }"
        )
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(ui_hex("panel")))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(ui_hex("text")))
        self.setPalette(palette)

    def sizeHint(self) -> QSize:
        return QSize(320, self._HINT_H)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, self._MIN_H)


def _paragraph(text: str, *, bold: bool = False) -> str:
    body = html.escape(text)
    if bold:
        body = f"<b>{body}</b>"
    return f"<p>{body}</p>"


def _append_protection(parts: list[str], message: str) -> None:
    """Bold the verdict. Branch rows become one indented list."""

    items: list[str] = []

    def flush() -> None:
        if not items:
            return
        lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
        parts.append(f"<ul>{lis}</ul>")
        items.clear()

    for raw in str(message).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Branch "):
            items.append(line)
            continue
        flush()
        parts.append(_paragraph(line, bold="is too small for" in line))
    flush()


class PowerDock(QWidget):
    export_requested = Signal()
    supply_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self._rows: list[dict] = []
        self._loading = False
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        self.caption = QLabel("Control power")
        head.addWidget(self.caption)
        head.addStretch()
        head.addWidget(QLabel("Margin"))
        self.margin = QDoubleSpinBox()
        self.margin.setRange(10.0, 100.0)
        self.margin.setDecimals(0)
        self.margin.setSuffix(" %")
        self.margin.setValue(80.0)
        self.margin.valueChanged.connect(self._margin_changed)
        head.addWidget(self.margin)
        self.export_btn = QPushButton("Export PDF")
        self.export_btn.clicked.connect(self.export_requested.emit)
        head.addWidget(self.export_btn)
        layout.addLayout(head)

        body = QHBoxLayout()
        self.supplies = QListWidget()
        self.supplies.setMaximumWidth(220)
        self.supplies.currentItemChanged.connect(self._supply_picked)
        body.addWidget(self.supplies)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._load_picked)
        self.table.setMinimumHeight(48)
        self.supplies.setMinimumHeight(48)
        body.addWidget(self.table, 1)
        layout.addLayout(body, 2)

        self.totals = SummaryText()
        layout.addWidget(self.totals, 1)

        edit = QHBoxLayout()
        edit.addStretch()
        edit.addWidget(QLabel("Estimate W"))
        self.watts = QDoubleSpinBox()
        self.watts.setRange(0.0, 10000.0)
        self.watts.setDecimals(2)
        self.watts.setSuffix(" W")
        edit.addWidget(self.watts)
        self.apply_btn = QPushButton("Apply estimate")
        self.apply_btn.clicked.connect(self._apply_estimate)
        edit.addWidget(self.apply_btn)
        layout.addLayout(edit)
        self.apply_theme()

    def apply_theme(self) -> None:
        from PySide6.QtGui import QColor, QPalette

        from panelcad.theme import ui_hex

        self.totals.apply_theme()
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(ui_hex("panel")))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(ui_hex("text")))
        self.setPalette(palette)

    def set_session(self, session: Session) -> None:
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        if self.session is None:
            return
        self._loading = True
        try:
            report = self.session.power_budget()
        except Exception as exc:  # noqa: BLE001 — dock must stay up
            self.caption.setText(f"Control power — {exc}")
            self._loading = False
            return
        self.margin.setValue(float(report.get("margin") or 0.8) * 100.0)
        current = self.current_supply()
        self.supplies.clear()
        self._rows = list(report.get("supplies") or [])
        for row in self._rows:
            flag = "" if row.get("ok_margin") and row.get("ok_inrush") and row.get("ok_protection") else "  !"
            label = f"{row.get('ref')}  {row.get('kind', '')}{flag}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row.get("ref"))
            self.supplies.addItem(item)
        if self._rows:
            self.select_supply(current or str(self._rows[0].get("ref") or ""))
        else:
            self.table.setRowCount(0)
            self.totals.setHtml(_paragraph("No control supply on this drawing."))
        count = len(self._rows)
        noun = "supply" if count == 1 else "supplies"
        self.caption.setText(f"Control power — {count} {noun}")
        self._loading = False
        self._clear_dock_tabs()

    def current_supply(self) -> str:
        item = self.supplies.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def select_supply(self, ref: str) -> None:
        token = str(ref or "").strip()
        if not token:
            return
        for index in range(self.supplies.count()):
            item = self.supplies.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == token:
                self.supplies.setCurrentItem(item)
                self._show(token)
                self.supply_changed.emit(token)
                return

    def _supply_picked(self, *_args) -> None:
        ref = self.current_supply()
        if ref:
            self._show(ref)

    def _show(self, ref: str) -> None:
        row = next((item for item in self._rows if item.get("ref") == ref), None)
        self.table.setRowCount(0)
        if row is None:
            self.totals.setHtml("")
            return
        loads = list(row.get("loads") or [])
        self.table.setRowCount(len(loads))
        for index, load in enumerate(loads):
            steady = f"{load.get('steady_w')} W"
            if load.get("kind") in {"coil", "relay"} and load.get("steady_va") and not load.get("estimate"):
                steady = f"{load.get('steady_va')} VA"
            inrush = _inrush_label(load)
            source = "estimate" if load.get("estimate") else (load.get("catalog") or "catalog")
            values = [
                str(load.get("ref") or ""),
                str(load.get("sheet") or ""),
                str(load.get("kind") or ""),
                steady,
                inrush,
                source,
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, load)
                self.table.setItem(index, col, cell)
        self.table.resizeColumnsToContents()
        parts: list[str] = []
        util = str(row.get("utilization") or "").strip()
        parts.append(_paragraph(f"{ref}: {util}" if util else f"{ref}: {row.get('steady_w')} W"))
        if row.get("boost_note"):
            parts.append(_paragraph(str(row["boost_note"])))
        if row.get("protection"):
            for item in row["protection"]:
                if item.get("message"):
                    _append_protection(parts, str(item["message"]))
        elif row.get("protection_note"):
            parts.append(_paragraph(str(row["protection_note"])))
        suggestion = row.get("suggestion") or {}
        if suggestion.get("catalog"):
            parts.append(_paragraph(f"Next supply: {suggestion.get('catalog')}"))
        elif row.get("warnings"):
            parts.append(_paragraph(str(row["warnings"][0])))
        self.totals.setHtml("".join(parts))

    def _load_picked(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        load = items[0].data(Qt.ItemDataRole.UserRole) or {}
        try:
            self.watts.setValue(float(load.get("steady_w") or 0.0))
        except (TypeError, ValueError):
            pass

    def _apply_estimate(self) -> None:
        if self.session is None:
            return
        items = self.table.selectedItems()
        if not items:
            return
        load = items[0].data(Qt.ItemDataRole.UserRole) or {}
        ref = str(load.get("ref") or "")
        sheet = str(load.get("sheet") or "") or None
        if not ref:
            return
        self.session.set_power_estimate(ref, power_w=float(self.watts.value()), sheet_id=sheet)
        self.refresh()

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self._clear_dock_tabs()

    def _clear_dock_tabs(self) -> None:
        """Keep the caption below a dock tab bar that lands on this widget."""

        window = self.window()
        if not isinstance(window, QMainWindow):
            return
        widget_top = self.mapTo(window, self.rect().topLeft()).y()
        extra = 0
        for bar in window.findChildren(QTabBar):
            if not bar.isVisible():
                continue
            origin = bar.mapTo(window, bar.rect().topLeft())
            if origin.y() >= widget_top + 8:
                continue
            overlap = origin.y() + bar.height() - widget_top
            if overlap > 0:
                extra = max(extra, overlap + 2)
        layout = self.layout()
        if layout is None:
            return
        top = 6 + int(extra)
        if layout.contentsMargins().top() != top:
            layout.setContentsMargins(6, top, 6, 6)
        hide = getattr(window, "_hide_duplicate_dock_tabs", None)
        if callable(hide):
            hide()

    def _margin_changed(self, value: float) -> None:
        if self._loading or self.session is None:
            return
        self.session.set_control_power_margin(float(value) / 100.0)
        self.refresh()
