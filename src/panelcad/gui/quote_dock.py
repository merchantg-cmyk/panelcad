"""Reports → Quote. Price list, labor table, and live totals. Files only."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.quote.labor import (
    EXAMPLE_LABEL,
    FILES_ONLY,
    count_phrase,
    labor_form_rows,
    labor_overrides,
    labor_patch,
    resolved_labor,
    uses_example_minutes,
)
from panelcad.quote.money import format_amount

_PART_COLUMNS = [
    "Item",
    "Qty",
    "Catalog",
    "Description",
    "Vendor",
    "Unit cost",
    "Extended",
    "Markup %",
    "Lead time",
    "Price date",
    "Age",
    "Flag",
]
# Vendor, unit cost, lead time, price date. Markup % is the category editor, not this grid.
_EDITABLE = {4, 5, 8, 9}
_PART_MONEY = {1, 5, 6, 7}


class QuoteDock(QWidget):
    """Shop quote panel. Export writes a file. Nothing is sent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: Session | None = None
        self._loading = False
        self._part_keys: list[str] = []
        self._labor_ids: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.banner = QLabel(
            f"{EXAMPLE_LABEL}. Not industry standards. {FILES_ONLY} "
            "Prices come only from a price list, a library part, or this grid."
        )
        self.banner.setWordWrap(True)
        self._banner_kind = "warning"
        self._totals_incomplete = False
        layout.addWidget(self.banner)
        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("quoteEmptyHint")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.hide()
        layout.addWidget(self.empty_hint)

        form = QFormLayout()
        self.number = QLineEdit()
        self.customer = QLineEdit()
        self.project_name = QLineEdit()
        self.mode = QComboBox()
        self.mode.addItem("Cheapest vendor", "cheapest")
        self.mode.addItem("Preferred vendor", "preferred")
        self.preferred = QLineEdit()
        self.parts_markup = QLineEdit()
        self.labor_markup = QLineEdit()
        self.tax = QLineEdit()
        self.freight = QLineEdit()
        self.contingency = QLineEdit()
        self.currency = QLineEdit()
        header = QHBoxLayout()
        for label, widget in (
            ("Quote", self.number),
            ("Customer", self.customer),
            ("Project", self.project_name),
            ("Prices", self.mode),
            ("Preferred", self.preferred),
            ("Currency", self.currency),
        ):
            header.addWidget(QLabel(label))
            header.addWidget(widget, 1)
        layout.addLayout(header)
        rates = QHBoxLayout()
        for label, widget in (
            ("Parts markup %", self.parts_markup),
            ("Labor markup %", self.labor_markup),
            ("Tax %", self.tax),
            ("Freight", self.freight),
            ("Contingency %", self.contingency),
        ):
            rates.addWidget(QLabel(label))
            rates.addWidget(widget)
        layout.addLayout(rates)
        _ = form

        buttons = QHBoxLayout()
        self.btn_import = QPushButton("Import price list…")
        self.btn_defaults = QPushButton("Save labor as my defaults")
        self.btn_xlsx = QPushButton("Export XLSX")
        self.btn_pdf = QPushButton("PDF internal")
        self.btn_customer = QPushButton("PDF customer")
        self.btn_csv = QPushButton("Export CSV")
        self.btn_customer_xlsx = QPushButton("XLSX customer")
        self.btn_customer_csv = QPushButton("CSV customer")
        for button in (
            self.btn_import,
            self.btn_defaults,
            self.btn_xlsx,
            self.btn_pdf,
            self.btn_customer,
            self.btn_customer_xlsx,
            self.btn_csv,
            self.btn_customer_csv,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.btn_import.clicked.connect(self._import_prices)
        self.btn_defaults.clicked.connect(self._save_defaults)
        self.btn_xlsx.clicked.connect(lambda: self._export("xlsx", "internal"))
        self.btn_pdf.clicked.connect(lambda: self._export("pdf", "internal"))
        self.btn_customer.clicked.connect(lambda: self._export("pdf", "customer"))
        self.btn_customer_xlsx.clicked.connect(lambda: self._export("xlsx", "customer"))
        self.btn_csv.clicked.connect(lambda: self._export("csv", "internal"))
        self.btn_customer_csv.clicked.connect(lambda: self._export("csv", "customer"))

        self.tabs = QTabWidget()
        self.parts = self._table(_PART_COLUMNS)
        self.parts.itemChanged.connect(self._part_edited)
        self.labor = self._table(["Line", "Value", "Wire min", "Unit"])
        self.labor.itemChanged.connect(self._labor_edited)
        self.markups = self._table(["Category", "Markup %"])
        self.markups.itemChanged.connect(self._markup_edited)
        self._markup_cats: list[str] = []
        self.breakdown = self._table(["Line", "Count", "Hours", "Cost", "Sell", "How computed"])
        self.breakdown.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabs.addTab(self.parts, "Parts")
        self.tabs.addTab(self.labor, "Labor rates")
        self.tabs.addTab(self.markups, "Category markup")
        self.tabs.addTab(self.breakdown, "Breakdown")
        layout.addWidget(self.tabs, 1)

        self.totals = QLabel("No quote yet")
        self.totals.setWordWrap(True)
        layout.addWidget(self.totals)
        self.apply_theme()

        for widget in (
            self.number,
            self.customer,
            self.project_name,
            self.preferred,
            self.parts_markup,
            self.labor_markup,
            self.tax,
            self.freight,
            self.contingency,
            self.currency,
        ):
            widget.editingFinished.connect(self._header_edited)
        self.mode.currentIndexChanged.connect(self._header_edited)

    def _table(self, columns: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        from panelcad.gui.theme import table_stylesheet

        table.setStyleSheet(table_stylesheet())
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        if columns and columns[0] == "Item":
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            # The Item column already numbers the lines.
            table.verticalHeader().setVisible(False)
        return table

    def apply_theme(self) -> None:
        from panelcad.gui.theme import banner_stylesheet, table_stylesheet

        for table in (self.parts, self.labor, self.markups, self.breakdown):
            table.setStyleSheet(table_stylesheet())
        self.banner.setStyleSheet(banner_stylesheet(getattr(self, "_banner_kind", "warning")))
        kind = "error" if getattr(self, "_totals_incomplete", False) else "neutral"
        self.totals.setStyleSheet(banner_stylesheet(kind))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        for table in (self.parts, self.labor, self.markups, self.breakdown):
            table.resizeRowsToContents()

    def set_session(self, session: Session) -> None:
        self._session = session
        self.refresh()

    def refresh(self) -> None:
        session = self._session
        if session is None:
            return
        self._loading = True
        try:
            quote = session.quote_build()
            self._fill_banner(quote)
            self._fill_header(quote)
            self._fill_parts(quote)
            self._fill_labor(session)
            self._fill_markups(quote)
            self._fill_breakdown(quote)
            self._fill_totals(quote)
        finally:
            self._loading = False

    def _fill_banner(self, quote: dict) -> None:
        prices = "Prices come only from a price list, a library part, or this grid."
        if quote.get("example_defaults"):
            self.banner.setText(f"{EXAMPLE_LABEL}. Not industry standards. {FILES_ONLY} {prices}")
            self._banner_kind = "warning"
        else:
            self.banner.setText(f"{FILES_ONLY} {prices}")
            self._banner_kind = "neutral"
        self.apply_theme()

    def _fill_header(self, quote: dict) -> None:
        self.number.setText(quote.get("number") or "")
        self.customer.setText(quote.get("customer") or "")
        self.project_name.setText(quote.get("project") or "")
        self.preferred.setText(quote.get("preferred_vendor") or "")
        self.currency.setText(quote.get("currency") or "")
        self.parts_markup.setText(_num(quote.get("parts_markup_percent")))
        self.labor_markup.setText(_num(quote.get("labor_markup_percent")))
        self.tax.setText(_num(quote.get("tax_percent")))
        self.freight.setText(_num(quote.get("freight")))
        self.contingency.setText(_num(quote.get("contingency_percent")))
        mode = quote.get("price_mode") or "cheapest"
        index = self.mode.findData(mode)
        if index >= 0:
            self.mode.setCurrentIndex(index)

    def _fill_parts(self, quote: dict) -> None:
        rows = quote["parts"]
        self._part_keys = [row["key"] for row in rows]
        self.parts.blockSignals(True)
        self.parts.setRowCount(len(rows))
        from panelcad.theme import ui_hex

        currency = str(quote.get("currency") or "")
        alert_bg = QColor(ui_hex("row_alert"))
        alert_fg = QColor(ui_hex("alert_text"))
        stale_bg = QColor(ui_hex("banner_warning_bg"))
        stale_fg = QColor(ui_hex("banner_warning_fg"))
        for r, row in enumerate(rows):
            values = [
                row["item"],
                str(row["qty"]),
                row["catalog"] or row["part_number"],
                row["description"],
                row["vendor"],
                "" if row["unit_cost"] is None else format_amount(row["unit_cost"], currency),
                "" if row["extended_cost"] is None else format_amount(row["extended_cost"], currency),
                _num(row["markup_percent"]),
                row["lead_time"],
                row["price_date"],
                row["age_label"],
                row["flag"],
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c not in _EDITABLE or row.get("needs_info"):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c in _PART_MONEY:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if row["missing_price"] and c in {10, 11}:
                    item.setBackground(alert_bg)
                    item.setForeground(alert_fg)
                elif row["stale"] and c in {10, 11}:
                    item.setBackground(stale_bg)
                    item.setForeground(stale_fg)
                self.parts.setItem(r, c, item)
        self.parts.blockSignals(False)
        self.parts.resizeRowsToContents()
        from panelcad.learn import quote_empty_copy

        hint = quote_empty_copy(len(rows))
        self.empty_hint.setText(hint)
        self.empty_hint.setVisible(bool(hint))

    def _fill_labor(self, session: Session) -> None:
        table = resolved_labor(session.project)
        rows = labor_form_rows(table)
        self._labor_ids = [row["id"] for row in rows]
        self.labor.blockSignals(True)
        self.labor.setRowCount(len(rows))
        for r, row in enumerate(rows):
            shown = row["label"]
            if uses_example_minutes(table, row["id"]):
                shown = f"{shown} — example default"
            label = QTableWidgetItem(shown)
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            value = QTableWidgetItem("" if row["value"] is None else str(row["value"]))
            second = QTableWidgetItem("" if row["kind"] != "device" else str(row["value2"] if row["value2"] is not None else ""))
            if row["kind"] != "device":
                second.setFlags(second.flags() & ~Qt.ItemFlag.ItemIsEditable)
            unit = QTableWidgetItem(str(row["unit"]))
            unit.setFlags(unit.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.labor.setItem(r, 0, label)
            self.labor.setItem(r, 1, value)
            self.labor.setItem(r, 2, second)
            self.labor.setItem(r, 3, unit)
        self.labor.blockSignals(False)

    def _fill_breakdown(self, quote: dict) -> None:
        rows = quote["labor"]
        currency = str(quote.get("currency") or "")
        self.breakdown.setRowCount(len(rows))
        for r, row in enumerate(rows):
            count = row["count"]
            if row["unit"] == "m":
                count_text = f"{float(count):.3f} m"
            else:
                count_text = count_phrase(float(count), str(row["unit"]))
            values = [
                row["description"],
                count_text,
                f"{row['hours']:.3f}",
                format_amount(row["cost"], currency),
                format_amount(row["sell"], currency),
                row["breakdown"],
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c in {1, 2, 3, 4}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.breakdown.setItem(r, c, item)
        self.breakdown.resizeRowsToContents()

    def _fill_totals(self, quote: dict) -> None:
        totals = quote["totals"]
        currency = str(quote.get("currency") or "")
        needs = str(quote.get("needs_info_label") or "").strip()
        needs_bit = f"   {needs}" if needs else ""
        self.totals.setText(
            f"Parts cost {format_amount(totals['parts_cost'], currency)}   "
            f"sell {format_amount(totals['parts_sell'], currency)}   "
            f"unpriced {quote['unpriced']}{needs_bit}   stale {quote['stale']}\n"
            f"Labor {totals['labor_hours']:.3f} h   "
            f"cost {format_amount(totals['labor_cost'], currency)}   "
            f"sell {format_amount(totals['labor_sell'], currency)}\n"
            f"{quote['parts_markup_label']}   "
            f"Freight {format_amount(totals['freight'], currency)}   "
            f"contingency {format_amount(totals['contingency'], currency)}   "
            f"tax {format_amount(totals['tax'], currency)}   "
            f"{quote['total_label']} {format_amount(totals['total'], currency)}"
        )
        self._totals_incomplete = bool(quote.get("incomplete"))
        self.apply_theme()

    def _header_edited(self) -> None:
        if self._loading or self._session is None:
            return
        try:
            self._session.quote_set_rates(
                number=self.number.text().strip(),
                customer=self.customer.text().strip(),
                project=self.project_name.text().strip(),
                currency=self.currency.text().strip(),
                price_mode=str(self.mode.currentData() or "cheapest"),
                preferred_vendor=self.preferred.text().strip(),
                parts_markup_percent=_blank_number(self.parts_markup.text()),
                labor_markup_percent=_blank_number(self.labor_markup.text()),
                tax_percent=_blank_number(self.tax.text()),
                freight=_blank_number(self.freight.text()),
                contingency_percent=_blank_number(self.contingency.text()),
            )
        except RuntimeError:
            # Focus can leave a field while Qt is deleting the panel.
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Quote", str(exc))
            return
        self.refresh()

    def _part_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or self._session is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._part_keys):
            return
        key = self._part_keys[row]
        column = item.column()
        text = item.text().strip()
        if column == 4:
            payload = {"vendor": text}
        elif column == 5:
            payload = {"unit_cost": text}
        elif column == 8:
            payload = {"lead_time": text}
        elif column == 9:
            payload = {"price_date": text}
        else:
            return
        try:
            self._session.quote_set_rates(manual={key: payload})
        except ValueError as exc:
            QMessageBox.warning(self, "Quote", str(exc))
        self.refresh()

    def _fill_markups(self, quote: dict) -> None:
        blob = {}
        session = self._session
        if session is not None:
            raw = session.project.properties.get("quote")
            if isinstance(raw, dict) and isinstance(raw.get("category_markups"), dict):
                blob = raw["category_markups"]
        cats: list[str] = []
        for row in quote["parts"]:
            cat = str(row.get("category") or "")
            if cat and cat not in cats:
                cats.append(cat)
        self._markup_cats = cats
        self.markups.blockSignals(True)
        self.markups.setRowCount(len(cats))
        for r, cat in enumerate(cats):
            label = QTableWidgetItem(cat)
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            stored = blob.get(cat, "")
            value = QTableWidgetItem("" if stored in (None, "") else _num(stored))
            value.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.markups.setItem(r, 0, label)
            self.markups.setItem(r, 1, value)
        self.markups.blockSignals(False)
        self.markups.resizeRowsToContents()

    def _markup_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or self._session is None or item.column() != 1:
            return
        row = item.row()
        if row < 0 or row >= len(self._markup_cats):
            return
        try:
            self._session.quote_set_rates(category_markups={self._markup_cats[row]: item.text().strip()})
        except ValueError as exc:
            QMessageBox.warning(self, "Quote", str(exc))
        self.refresh()

    def _labor_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or self._session is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._labor_ids):
            return
        row_id = self._labor_ids[row]
        value = self.labor.item(row, 1).text().strip() if self.labor.item(row, 1) else ""
        value2 = self.labor.item(row, 2).text().strip() if self.labor.item(row, 2) else ""
        if not value:
            return
        try:
            patch = labor_patch(row_id, value, value2 or None, resolved_labor(self._session.project))
            self._session.quote_set_rates(labor=patch, scope="project")
        except ValueError as exc:
            QMessageBox.warning(self, "Quote", str(exc))
        self.refresh()

    def _import_prices(self) -> None:
        if self._session is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import price list",
            "",
            "Price lists (*.csv *.xlsx)",
        )
        if not path:
            return
        try:
            result = self._session.quote_import_prices(
                path,
                price_mode=str(self.mode.currentData() or "cheapest"),
                preferred_vendor=self.preferred.text().strip(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Price list", str(exc))
            return
        self.refresh()
        skipped = result.get("skipped") or []
        extra = ""
        if skipped:
            extra = "\n" + "\n".join(str(line) for line in skipped[:8])
        QMessageBox.information(
            self,
            "Price list",
            f"Imported {result['rows']} prices from {Path(path).name} ({result.get('encoding') or 'unknown'}). Nothing was sent.{extra}",
        )

    def _save_defaults(self) -> None:
        if self._session is None:
            return
        table = resolved_labor(self._session.project)
        overrides = labor_overrides(table)
        if overrides:
            self._session.quote_set_rates(labor=overrides, scope="user", clear_project_labor=True)
        else:
            self._session.quote_set_rates(clear_project_labor=True)
        self.refresh()
        QMessageBox.information(
            self,
            "Labor defaults",
            "Saved the labor numbers you changed. Values still equal to the shipped "
            "placeholders stay labelled example defaults. Nothing was sent.",
        )

    def _export(self, kind: str, view: str) -> None:
        if self._session is None:
            return
        if view == "customer":
            quote = self._session.quote_build()
            if quote.get("example_defaults"):
                QMessageBox.warning(
                    self,
                    "Quote",
                    f"{EXAMPLE_LABEL}. This customer copy still uses those placeholders.",
                )
            if quote.get("incomplete"):
                answer = QMessageBox.question(
                    self,
                    "Quote",
                    f"{quote['total_label']}. Write the customer file anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        filters = {
            "xlsx": "Excel (*.xlsx)",
            "pdf": "PDF (*.pdf)",
            "csv": "CSV (*.csv)",
        }
        path, _ = QFileDialog.getSaveFileName(self, "Export quote", f"quote.{kind}", filters[kind])
        if not path:
            return
        try:
            result = self._session.quote_export(path, fmt=kind, view=view)
        except ValueError as exc:
            QMessageBox.warning(self, "Quote", str(exc))
            return
        QMessageBox.information(self, "Quote", f"Wrote {result['path']}\n{FILES_ONLY}")


def _num(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _blank_number(text: str) -> float:
    raw = text.strip()
    if not raw:
        return 0.0
    return float(raw)
