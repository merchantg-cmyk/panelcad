from __future__ import annotations

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag
from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.gui.canvas import LIBRARY_MIME
from panelcad.library.catalog import Catalog


def _spacing_fields(cutout) -> tuple[float, float, bool]:
    """Horizontal mm, vertical mm, and whether the two pitches may be swapped."""

    if cutout.centers_interchangeable and cutout.min_center_a_mm and cutout.min_center_b_mm:
        return float(cutout.min_center_a_mm), float(cutout.min_center_b_mm), True
    if cutout.min_center_x_mm and cutout.min_center_y_mm:
        return float(cutout.min_center_x_mm), float(cutout.min_center_y_mm), False
    if cutout.min_center_a_mm and cutout.min_center_b_mm:
        return float(cutout.min_center_a_mm), float(cutout.min_center_b_mm), True
    return 0.0, 0.0, False


_SHOP_NOTE = "Shop opening entered on the device. Not a cited manufacturer drawing."

# Library tree groups. The stored category id stays; the dock shows words.
_CATEGORY_LABELS = {
    "connection.accessory": "Connection accessories",
    "connection.bus": "Bus bars",
    "connection.duct": "Wire duct",
    "connection.ground": "Ground",
    "connection.network": "Network",
    "connection.node": "Nodes",
    "connection.notes": "Notes",
    "connection.offpage": "Off-page arrows",
    "connection.terminal": "Terminals",
    "control.hmi": "HMIs",
    "control.plc": "PLCs",
    "drive.softstarter": "Soft starters",
    "drive.vfd": "Drives",
    "load.fan": "Fans",
    "load.motor": "Motors",
    "load.solenoid": "Solenoids",
    "load.transmitter": "Transmitters",
    "mechanical.envelope": "Measured envelopes",
    "operator.estop": "E-stops",
    "operator.light": "Lights",
    "operator.limit": "Limit switches",
    "operator.pushbutton": "Pushbuttons",
    "operator.selector": "Selector switches",
    "power.device": "Power devices",
    "power.load": "Power loads",
    "power.receptacle": "Receptacles",
    "power.supply": "Power supplies",
    "power.transformer": "Transformers",
    "protection.breaker": "Breakers",
    "protection.disconnect": "Disconnects",
    "protection.fuse": "Fuses",
    "protection.mpcb": "Motor protectors",
    "protection.overload": "Overloads",
    "sensor.proximity": "Proximity switches",
    "switching.aux": "Auxiliary contacts",
    "switching.contactor": "Contactors",
    "switching.interlock": "Interlocks",
    "switching.relay": "Relays",
    "switching.safety": "Safety relays",
    "timing.timer": "Timers",
}


def category_label(category: str) -> str:
    key = str(category or "").strip()
    return _CATEGORY_LABELS.get(key, key)


def _holes_text(cutout) -> str:
    return "; ".join(f"{hole.diameter_mm:g} {hole.x_mm:g} {hole.y_mm:g}" for hole in cutout.holes)


def _parse_holes(text: str) -> list[dict]:
    holes: list[dict] = []
    for raw in str(text or "").replace(";", "\n").splitlines():
        parts = raw.replace(",", " ").split()
        if not parts:
            continue
        if len(parts) < 3:
            raise ValueError("a drill needs diameter, x, and y")
        holes.append({"diameter_mm": float(parts[0]), "x_mm": float(parts[1]), "y_mm": float(parts[2])})
    return holes


def _wrapped_text_height(label: QLabel, width: int) -> int:
    flags = int(Qt.TextFlag.TextWordWrap)
    return label.fontMetrics().boundingRect(0, 0, max(20, width), 4000, flags, label.text()).height() + 4


class _WrapLabel(QLabel):
    """Word-wrapped label that reports the height its text needs."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return _wrapped_text_height(self, width)

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self.width() if self.width() > 20 else 160
        return QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        needed = self.heightForWidth(max(20, self.width()))
        if self.minimumHeight() != needed:
            self.setMinimumHeight(needed)
            self.updateGeometry()


class _WrapHost(QWidget):
    """Checkbox plus a wrapping label whose row grows when the inspector is narrow."""

    def __init__(self, text: str, object_name: str, tip: str = "") -> None:
        super().__init__()
        self.setObjectName(object_name + "Row")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._box = QCheckBox()
        self._box.setObjectName(object_name)
        self._label = _WrapLabel(text)
        self._label.setObjectName(object_name + "Label")
        if tip:
            self._box.setToolTip(tip)
            self._label.setToolTip(tip)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(6)
        row.addWidget(self._box, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._label, 1)
        self._row = row

        def _toggle(event, target: QCheckBox = self._box, label: QLabel = self._label) -> None:
            if target.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
                target.toggle()
            else:
                QLabel.mousePressEvent(label, event)

        self._label.mousePressEvent = _toggle  # type: ignore[method-assign]

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        box_w = self._box.sizeHint().width()
        label_w = max(20, int(width) - box_w - self._row.spacing())
        flags = int(Qt.TextFlag.TextWordWrap)
        text_h = self._label.fontMetrics().boundingRect(0, 0, label_w, 4000, flags, self._label.text()).height()
        return max(self._box.sizeHint().height(), text_h) + 6

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        needed = self.heightForWidth(max(20, self.width()))
        if self.minimumHeight() != needed:
            self.setMinimumHeight(needed)
            self.updateGeometry()


def _wrap_check(text: str, object_name: str, tip: str = "") -> tuple[QWidget, QCheckBox]:
    """Checkbox plus a wrapping label, so a narrow inspector does not clip the words."""

    host = _WrapHost(text, object_name, tip)
    return host, host._box


class LibraryTree(QTreeWidget):
    """Copy-only drag. Never move/delete rows out of the library."""

    device_dragged = Signal(str)

    def startDrag(self, supportedActions) -> None:  # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        lib_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not lib_id:
            return
        self.device_dragged.emit(str(lib_id))
        mime = QMimeData()
        raw = str(lib_id).encode("utf-8")
        mime.setData(LIBRARY_MIME, raw)
        mime.setText(str(lib_id))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class LibraryDock(QWidget):
    device_chosen = Signal(str)
    place_now_requested = Signal(str)
    new_requested = Signal()
    edit_requested = Signal(str)
    duplicate_requested = Signal(str)
    parts_requested = Signal()

    def __init__(self, catalog: Catalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        layout = QVBoxLayout(self)
        from panelcad.theme import MARGIN_PX, SPACING_PX

        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        layout.addWidget(QLabel("Library — drag onto the sheet"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("search breakers, plc, hmi…")
        self.search.setMinimumWidth(0)
        self.search.textChanged.connect(self._rebuild)
        layout.addWidget(self.search)
        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("libraryEmptyHint")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.hide()
        layout.addWidget(self.empty_hint)
        lib_btns = QGridLayout()
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self.new_requested.emit)
        edit_btn = QPushButton("Edit…")
        edit_btn.clicked.connect(self._edit_current)
        dup_btn = QPushButton("Duplicate…")
        dup_btn.clicked.connect(self._duplicate_current)
        parts_btn = QPushButton("Catalog parts…")
        parts_btn.setToolTip("Search verified manufacturer catalog numbers")
        parts_btn.clicked.connect(self.parts_requested.emit)
        # New… and Edit… share a row. Duplicate… and Catalog parts… are each
        # wider than half of a 200 px library, so each gets its own full row.
        for button in (new_btn, edit_btn, dup_btn, parts_btn):
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        lib_btns.setHorizontalSpacing(SPACING_PX)
        lib_btns.setVerticalSpacing(SPACING_PX)
        lib_btns.setContentsMargins(0, 0, 0, 0)
        lib_btns.addWidget(new_btn, 0, 0)
        lib_btns.addWidget(edit_btn, 0, 1)
        lib_btns.addWidget(dup_btn, 1, 0, 1, 2)
        lib_btns.addWidget(parts_btn, 2, 0, 1, 2)
        lib_btns.setColumnStretch(0, 1)
        lib_btns.setColumnStretch(1, 1)
        layout.addLayout(lib_btns)
        self.tree = LibraryTree()
        self.tree.setHeaderHidden(True)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QTreeWidget.DragDropMode.DragOnly)
        self.tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.viewport().customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemPressed.connect(self._clicked)
        self.tree.device_dragged.connect(self.device_chosen.emit)
        layout.addWidget(self.tree)
        self._rebuild()

    def _rebuild(self) -> None:
        self.tree.clear()
        query = self.search.text()
        devices = self.catalog.search(query)
        groups: dict[str, QTreeWidgetItem] = {}
        for d in devices:
            if d.category not in groups:
                groups[d.category] = QTreeWidgetItem([category_label(d.category)])
                self.tree.addTopLevelItem(groups[d.category])
            child = QTreeWidgetItem([f"{d.name}  ({d.library_id})"])
            child.setData(0, Qt.ItemDataRole.UserRole, d.library_id)
            child.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            groups[d.category].addChild(child)
        if query:
            self.tree.expandAll()
        from panelcad.learn import library_search_empty

        hint = library_search_empty(query, len(devices))
        self.empty_hint.setText(hint)
        self.empty_hint.setVisible(bool(hint))

    def _current_lib_id(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        lib_id = item.data(0, Qt.ItemDataRole.UserRole)
        return str(lib_id) if lib_id else None

    def _edit_current(self) -> None:
        lib_id = self._current_lib_id()
        if lib_id:
            self.edit_requested.emit(lib_id)

    def _duplicate_current(self) -> None:
        lib_id = self._current_lib_id()
        if lib_id:
            self.duplicate_requested.emit(lib_id)

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        lib_id = item.data(0, Qt.ItemDataRole.UserRole)
        if lib_id:
            self.device_chosen.emit(str(lib_id))

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        menu.addAction("Symbol Builder…", self.new_requested.emit)
        lib_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if lib_id:
            menu.addAction("Edit symbol…", lambda: self.edit_requested.emit(str(lib_id)))
            menu.addAction("Duplicate…", lambda: self.duplicate_requested.emit(str(lib_id)))
            menu.addSeparator()
            menu.addAction("Place on sheet", lambda: self.place_now_requested.emit(str(lib_id)))
            menu.addAction("Arm place mode", lambda: self.device_chosen.emit(str(lib_id)))
        menu.exec(self.tree.viewport().mapToGlobal(pos))


class PropertiesDock(QWidget):
    select_requested = Signal(str)
    edit_requested = Signal(str)
    address_requested = Signal(str)
    delete_requested = Signal(str)
    catalog_requested = Signal(str)
    envelope_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        on_change: Callable[[], None] | None = None,
        on_logo: Callable[[], None] | None = None,
        on_clear_logo: Callable[[], None] | None = None,
        on_component_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self._session: Session | None = None
        self._on_change = on_change
        self._on_logo = on_logo
        self._on_clear_logo = on_clear_logo
        self._on_component_change = on_component_change
        layout = QVBoxLayout(self)
        from panelcad.theme import MARGIN_PX, SPACING_PX

        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        self.heading = QLabel("Drawing info (title block)")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.form_host = QWidget()
        self.form_host.setMinimumWidth(0)
        self.form = QFormLayout(self.form_host)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.form.setContentsMargins(0, 0, 0, 0)
        self._loading_cutout = False
        layout.addWidget(self.form_host)
        layout.addWidget(QLabel("Devices on this sheet — right-click"))
        self.list = QListWidget()
        self.list.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.viewport().customContextMenuRequested.connect(self._list_menu)
        self.list.itemClicked.connect(self._list_clicked)
        layout.addWidget(self.list)
        layout.addStretch()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        for name in ("cutoutUnknownRow", "cutoutIncludeKeyRow", "cutoutPitchSwapRow"):
            host = self.findChild(QWidget, name)
            if host is None or not host.hasHeightForWidth():
                continue
            needed = host.heightForWidth(max(40, host.width()))
            if host.minimumHeight() != needed:
                host.setMinimumHeight(needed)
                host.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(min(hint.width(), 220), hint.height())

    def show_project(self, session: Session) -> None:
        self._session = session
        self.list.clear()
        for c in session.sheet().components:
            row = QListWidgetItem(f"{c.id}  {c.library_id}")
            row.setData(Qt.ItemDataRole.UserRole, c.id)
            self.list.addItem(row)
        self.heading.setText("Drawing info (title block)")
        tb = session.project.title_block
        sheet = session.sheet()
        rev = tb.revisions[0] if tb.revisions else None
        self._set_edits(
            [
                ("Project", session.project.name, "name"),
                ("Title", sheet.title, "title"),
                ("Description", sheet.description, "description"),
                ("Company", tb.company, "company"),
                ("Address", tb.address, "address"),
                ("Customer", tb.customer, "customer"),
                ("Location", tb.location, "location"),
                ("Dwg No.", tb.drawing_number, "drawing_number"),
                ("Drawn", tb.drawn_by, "drawn_by"),
                ("Checked", tb.checked_by, "checked_by"),
                ("Approved", tb.approved_by, "approved_by"),
                ("Rev", rev.rev if rev else "", "rev"),
                ("Rev date", rev.date if rev else "", "date"),
                ("Rev note", rev.description if rev else "", "rev_description"),
                ("Scale", tb.scale, "scale"),
            ]
        )
        self._add_pref_combo(
            "Standard",
            ["IEC", "NEMA/ANSI", "OEM"],
            tb.standard,
            "standard",
        )
        self._add_pref_combo(
            "Drawing style",
            ["schematic", "ladder", "freeform"],
            tb.drawing_style or "schematic",
            "drawing_style",
        )
        self._add_pref_combo(
            "Coil style",
            ["circle", "square"],
            tb.coil_style,
            "coil_style",
        )
        sheet = session.sheet()
        if sheet.kind in {"schematic", "io"} and sheet.control_voltage is not None:
            stated = sheet.control_voltage
        else:
            stated = session.project.control_voltage
        for label, value, field in (
            ("Rail (left)", tb.rail_hot or "L1", "rail_hot"),
            ("Rail (right)", tb.rail_return or "L2", "rail_return"),
            ("Control voltage", "" if stated is None else f"{float(stated):g}", "control_voltage"),
        ):
            edit = QLineEdit(value or "")
            edit.editingFinished.connect(lambda f=field, e=edit: self._commit(f, e.text(), e))
            self.form.addRow(label, edit)
        numbering = session.wire_numbering()
        self._add_numbering_combo(numbering)
        self._add_xref_combo(session.cross_reference_settings())
        self._add_phase_row()
        self._add_logo_row(tb.logo)

    def show_component(self, session: Session, ref: str) -> None:
        self._session = session
        comp = None
        home = None
        try:
            home = session.sheet()
            comp = home.component(ref)
        except KeyError:
            comp = None
            for sheet in session.project.sheets:
                try:
                    comp = sheet.component(ref)
                    home = sheet
                    break
                except KeyError:
                    continue
        if comp is None or home is None:
            self.show_project(session)
            return
        self.heading.setText(comp.id)
        on_sheet = home.id == session.sheet().id
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            self.show_project(session)
            return
        from panelcad.library.panel import effective_footprint

        fp = effective_footprint(device, comp)
        rows = [
            ("Ref", comp.id),
            ("Sheet", home.id),
            ("Library", comp.library_id),
            ("Name", device.name),
            ("x mm", f"{comp.x:.1f}"),
            ("y mm", f"{comp.y:.1f}"),
            ("rotation", str(comp.rotation)),
            ("DIN rail", "yes" if fp.din_rail else "no"),
            ("In panel", "yes" if fp.in_panel else "no"),
            ("Mount", fp.mount),
            ("W × H × D", f"{fp.width_mm:g} × {fp.height_mm:g} × {fp.depth_mm:g} mm"),
            ("Dim source", fp.dim_source),
        ]
        skip_props = {
            "ip",
            "address",
            "node",
            "station",
            "cutout",
            "include_key",
            "min_center_x_mm",
            "min_center_y_mm",
            "centers_interchangeable",
            "cutout_restore",
        }
        for k, v in {**device.properties, **comp.properties}.items():
            if k in skip_props:
                continue
            rows.append((str(k), str(v)))
        self._set_form(rows)
        pick = QPushButton("Catalog part…")
        pick.setToolTip("Assign a verified manufacturer catalog number")
        pick.clicked.connect(lambda r=ref: self.catalog_requested.emit(r))
        self.form.addRow("", pick)
        envelope = QPushButton("Confirm envelope…")
        envelope.setToolTip("Enter and confirm width, height, depth, and mounting for this part")
        envelope.clicked.connect(lambda r=ref: self.envelope_requested.emit(r))
        self.form.addRow("", envelope)
        source = str(comp.properties.get("catalog_source") or "")
        if source:
            link = QLabel(f'<a href="{source}">Official source</a>')
            link.setOpenExternalLinks(True)
            link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            self.form.addRow("Source", link)
        if not on_sheet:
            return
        spin = QDoubleSpinBox()
        spin.setRange(0.15, 8.0)
        spin.setSingleStep(0.05)
        spin.setDecimals(2)
        spin.blockSignals(True)
        spin.setValue(comp.visual_scale())
        spin.blockSignals(False)
        spin.setMinimumWidth(0)
        spin.setToolTip("1.00 is the library size. Drag the orange handle on the sheet, or type here.")
        spin.valueChanged.connect(lambda v, r=ref: self._commit_scale(r, v))
        self.form.addRow("Size", spin)
        lock = QCheckBox("Lock connection points")
        lock.setMinimumWidth(0)
        lock.blockSignals(True)
        lock.setChecked(comp.pins_locked)
        lock.blockSignals(False)
        lock.setToolTip("Unlock to drag the red terminals onto the real connection point, then lock again.")
        lock.toggled.connect(lambda on, r=ref: self._commit_pins_locked(r, on))
        self.form.addRow("", lock)
        ip_edit = QLineEdit(str(comp.properties.get("ip") or comp.properties.get("address") or ""))
        ip_edit.setPlaceholderText("192.168.1.10")
        ip_edit.editingFinished.connect(lambda r=ref, e=ip_edit: self._commit_address(r, ip=e.text()))
        self.form.addRow("IP / address", ip_edit)
        node_edit = QLineEdit(str(comp.properties.get("node") or comp.properties.get("station") or ""))
        node_edit.setPlaceholderText("1")
        node_edit.editingFinished.connect(lambda r=ref, e=node_edit: self._commit_address(r, node=e.text()))
        self.form.addRow("Node / station", node_edit)
        self._add_cutout_editor(ref, comp)

    def _add_cutout_editor(self, ref: str, comp) -> None:
        """Shop opening, key, and centre minimums. Every control calls ``part_set_cutout``."""

        from panelcad.core.cutout import resolve_cutout, size_label

        if self._session is None:
            return
        cutout = resolve_cutout(comp.library_id, comp.properties)
        if cutout is None:
            return
        include = comp.properties.get("include_key")
        if include is None and isinstance(comp.properties.get("cutout"), dict):
            include = comp.properties["cutout"].get("include_key")
        key_on = cutout.cuts_key(None if include is None else bool(include))
        heading = QLabel(size_label(cutout, include_key=key_on))
        heading.setWordWrap(True)
        self.form.addRow("Cutout", heading)
        if not cutout.verified and not cutout.unknown:
            unverified = QLabel("UNVERIFIED")
            unverified.setObjectName("cutoutUnverified")
            unverified.setToolTip(
                "These figures were not checked on an official publication. "
                "The IEC 60947-5-1 numbers are transcribed from a secondary copy."
            )
            self.form.addRow("Citation", unverified)

        unknown_host, unknown = _wrap_check(
            "Unknown — do not cut a hole",
            "cutoutUnknown",
            "No hole is cut. Unchecking restores the opening, key, and centre minimums.",
        )
        unknown.blockSignals(True)
        unknown.setChecked(bool(cutout.unknown))
        unknown.blockSignals(False)
        unknown.toggled.connect(lambda on, r=ref: self._commit_cutout_unknown(r, on))
        self.form.addRow("", unknown_host)

        self._loading_cutout = True
        shape = QComboBox()
        shape.setObjectName("cutoutShape")
        shape.addItem("Round", "round")
        shape.addItem("Rectangle", "rect")
        shape.addItem("D", "d")
        current_shape = cutout.shape if cutout.shape in {"round", "rect", "d"} else "round"
        shape.setCurrentIndex(max(0, shape.findData(current_shape)))
        diameter = self._size_spin("cutoutDiameter", "Hole diameter, mm.")
        width = self._size_spin("cutoutWidth", "Opening width, mm.")
        height = self._size_spin("cutoutHeight", "Opening height, mm.")
        plus = self._size_spin("cutoutPlus", "Plus tolerance, mm. Applied to the diameter, or to both sides of a rectangle.")
        minus = self._size_spin("cutoutMinus", "Minus tolerance, mm. Zero is 0, not unknown.", blank_zero=False)
        key_width = self._size_spin("cutoutKeyWidth", "Key notch width, mm.")
        key_span = self._size_spin("cutoutKeySpan", "Key notch span from the far side of the hole, mm.")
        corner = self._size_spin("cutoutCorner", "Corner radius, mm. Zero is a square corner.", blank_zero=False)
        clearance = self._size_spin("cutoutClearance", "Extra clearance around this opening, mm. Blank means none.")
        depth = self._size_spin("cutoutDepth", "Depth behind the panel, mm. Blank means none on file.")
        holes = QLineEdit()
        holes.setObjectName("cutoutHoles")
        holes.setPlaceholderText("diameter x y; diameter x y")
        holes.setToolTip("Mounting drills from the opening centre, Y down. One triple per hole, separated by semicolons.")
        plus.setRange(0.0, 50.0)
        minus.setRange(0.0, 50.0)
        corner.setRange(0.0, 100.0)
        clearance.setRange(0.0, 500.0)
        depth.setRange(0.0, 2000.0)
        if cutout.shape == "rect":
            width.setValue(float(cutout.width_mm or 0))
            height.setValue(float(cutout.height_mm or 0))
            plus.setValue(float(cutout.width_plus_mm or 0))
            minus.setValue(float(cutout.width_minus_mm or 0))
        else:
            diameter.setValue(float(cutout.diameter_mm or 0))
            plus.setValue(float(cutout.diameter_plus_mm or 0))
            minus.setValue(float(cutout.diameter_minus_mm or 0))
        key_width.setValue(float(cutout.key_width_mm or 0))
        key_span.setValue(float(cutout.key_span_mm or 0))
        corner.setValue(float(cutout.corner_radius_mm or 0))
        clearance.setValue(float(cutout.clearance_mm or 0))
        depth.setValue(float(cutout.depth_behind_mm or 0))
        holes.setText(_holes_text(cutout))
        shop = (shape, diameter, width, height, plus, minus, key_width, key_span, corner, clearance, depth)
        for spin in shop[1:]:
            spin.editingFinished.connect(lambda r=ref: self._commit_shop(r))
        holes.editingFinished.connect(lambda r=ref: self._commit_shop(r))
        shape.currentIndexChanged.connect(lambda _index, r=ref: self._commit_shop(r))
        self.form.addRow("Shape", shape)
        self.form.addRow("Diameter", diameter)
        self.form.addRow("Width", width)
        self.form.addRow("Height", height)
        self.form.addRow("Plus", plus)
        self.form.addRow("Minus", minus)
        self.form.addRow("Key width", key_width)
        self.form.addRow("Key span", key_span)
        self.form.addRow("Corner radius", corner)
        self.form.addRow("Clearance", clearance)
        self.form.addRow("Depth behind", depth)
        self.form.addRow("Drills", holes)
        self._show_shop_fields(current_shape)
        self._loading_cutout = False

        key_host, key = _wrap_check(
            "Include key notch",
            "cutoutIncludeKey",
            "Off leaves the round hole. Allen-Bradley marks the key optional.",
        )
        key.setEnabled(bool(cutout.key_width_mm and cutout.key_span_mm) and not cutout.unknown)
        key.blockSignals(True)
        key.setChecked(key_on)
        key.blockSignals(False)
        key.toggled.connect(lambda on, r=ref: self._commit_include_key(r, on))
        self.form.addRow("", key_host)

        horizontal, vertical, swapped = _spacing_fields(cutout)
        x_spin = self._spacing_spin(horizontal, "Minimum centre distance, horizontal, from the part's drawing.")
        y_spin = self._spacing_spin(vertical, "Minimum centre distance, vertical, from the part's drawing.")
        x_spin.setObjectName("cutoutMinX")
        y_spin.setObjectName("cutoutMinY")
        swap_host, swap = _wrap_check(
            "Row and column pitches may be swapped",
            "cutoutPitchSwap",
            "The row and column distances may trade places. IEC 60947-5-1 Table 3 says that; that table was not checked on an official IEC publication.",
        )
        swap.blockSignals(True)
        swap.setChecked(swapped)
        swap.blockSignals(False)
        enabled = not cutout.unknown
        shop_widgets = (shape, diameter, width, height, plus, minus, key_width, key_span, corner, clearance, depth, holes)
        for widget in (*shop_widgets, x_spin, y_spin, swap):
            widget.setEnabled(enabled and widget.isEnabled())
        if not enabled:
            for widget in shop_widgets:
                widget.setEnabled(False)
        x_spin.editingFinished.connect(
            lambda r=ref, xs=x_spin, ys=y_spin, box=swap: self._commit_spacing(r, xs.value(), ys.value(), box.isChecked())
        )
        y_spin.editingFinished.connect(
            lambda r=ref, xs=x_spin, ys=y_spin, box=swap: self._commit_spacing(r, xs.value(), ys.value(), box.isChecked())
        )
        swap.toggled.connect(
            lambda _on, r=ref, xs=x_spin, ys=y_spin, box=swap: self._commit_spacing(
                r, xs.value(), ys.value(), box.isChecked()
            )
        )
        self.form.addRow("Min centre X", x_spin)
        self.form.addRow("Min centre Y", y_spin)
        self.form.addRow("", swap_host)

    def _size_spin(self, object_name: str, tip: str, *, blank_zero: bool = True) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setObjectName(object_name)
        spin.setRange(0.0, 2000.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setSuffix(" mm")
        if blank_zero:
            spin.setSpecialValueText("—")
        spin.setToolTip(tip)
        return spin

    def _show_shop_fields(self, shape: str) -> None:
        form = self.form
        diameter = self.findChild(QDoubleSpinBox, "cutoutDiameter")
        width = self.findChild(QDoubleSpinBox, "cutoutWidth")
        height = self.findChild(QDoubleSpinBox, "cutoutHeight")
        key_width = self.findChild(QDoubleSpinBox, "cutoutKeyWidth")
        key_span = self.findChild(QDoubleSpinBox, "cutoutKeySpan")
        corner = self.findChild(QDoubleSpinBox, "cutoutCorner")
        if diameter is None:
            return
        rect = shape == "rect"
        for widget, visible in (
            (diameter, not rect),
            (width, rect),
            (height, rect),
            (key_width, shape == "d"),
            (key_span, shape == "d"),
            (corner, rect),
        ):
            if widget is None:
                continue
            widget.setVisible(visible)
            label = form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

    def _commit_shop(self, ref: str) -> None:
        if self._loading_cutout or self._session is None:
            return
        from panelcad.core.cutout import parse_cutout, resolve_cutout

        shape_box = self.findChild(QComboBox, "cutoutShape")
        if shape_box is None:
            return
        shape = str(shape_box.currentData() or "round")
        self._show_shop_fields(shape)
        diameter = self.findChild(QDoubleSpinBox, "cutoutDiameter")
        width = self.findChild(QDoubleSpinBox, "cutoutWidth")
        height = self.findChild(QDoubleSpinBox, "cutoutHeight")
        plus = self.findChild(QDoubleSpinBox, "cutoutPlus")
        minus = self.findChild(QDoubleSpinBox, "cutoutMinus")
        key_width = self.findChild(QDoubleSpinBox, "cutoutKeyWidth")
        key_span = self.findChild(QDoubleSpinBox, "cutoutKeySpan")
        corner = self.findChild(QDoubleSpinBox, "cutoutCorner")
        clearance = self.findChild(QDoubleSpinBox, "cutoutClearance")
        depth = self.findChild(QDoubleSpinBox, "cutoutDepth")
        holes = self.findChild(QLineEdit, "cutoutHoles")
        if None in (diameter, width, height, plus, minus, key_width, key_span, corner, clearance, depth, holes):
            return
        comp = None
        for sheet in self._session.project.sheets:
            try:
                comp = sheet.component(ref)
                break
            except KeyError:
                continue
        if comp is None:
            return
        current = resolve_cutout(comp.library_id, comp.properties)
        try:
            hole_rows = _parse_holes(holes.text())
        except ValueError:
            return
        plus_mm = float(plus.value())
        minus_mm = float(minus.value())
        data: dict = {"unknown": False, "shape": shape, "holes": hole_rows}
        if clearance.value() > 0:
            data["clearance_mm"] = float(clearance.value())
        if depth.value() > 0:
            data["depth_behind_mm"] = float(depth.value())
        changed = current is None or current.unknown or current.shape != shape
        if shape == "rect":
            if width.value() <= 0 or height.value() <= 0:
                return
            data["width_mm"] = float(width.value())
            data["height_mm"] = float(height.value())
            data["width_plus_mm"] = plus_mm
            data["height_plus_mm"] = plus_mm
            data["width_minus_mm"] = minus_mm
            data["height_minus_mm"] = minus_mm
            data["corner_radius_mm"] = float(corner.value())
            if current is not None and not current.unknown and current.shape == "rect":
                changed = changed or abs(float(current.width_mm or 0) - data["width_mm"]) > 0.05
                changed = changed or abs(float(current.height_mm or 0) - data["height_mm"]) > 0.05
                changed = changed or abs(float(current.width_plus_mm or 0) - plus_mm) > 0.005
                changed = changed or abs(float(current.width_minus_mm or 0) - minus_mm) > 0.005
                changed = changed or abs(float(current.corner_radius_mm or 0) - data["corner_radius_mm"]) > 0.05
        else:
            if diameter.value() <= 0:
                return
            if shape == "d" and (key_width.value() <= 0 or key_span.value() <= 0):
                return
            data["diameter_mm"] = float(diameter.value())
            data["diameter_plus_mm"] = plus_mm
            data["diameter_minus_mm"] = minus_mm
            if current is not None and not current.unknown and current.shape == shape:
                changed = changed or abs(float(current.diameter_mm or 0) - data["diameter_mm"]) > 0.05
                changed = changed or abs(float(current.diameter_plus_mm or 0) - plus_mm) > 0.005
                changed = changed or abs(float(current.diameter_minus_mm or 0) - minus_mm) > 0.005
            if shape == "d":
                data["key_width_mm"] = float(key_width.value())
                data["key_span_mm"] = float(key_span.value())
                data["key_side"] = current.key_side if current is not None else "top"
                data["key_optional"] = False
                if current is not None:
                    data["key_width_plus_mm"] = float(current.key_width_plus_mm or 0)
                    data["key_span_plus_mm"] = float(current.key_span_plus_mm or 0)
                if current is not None and not current.unknown:
                    changed = changed or abs(float(current.key_width_mm or 0) - data["key_width_mm"]) > 0.05
                    changed = changed or abs(float(current.key_span_mm or 0) - data["key_span_mm"]) > 0.05
            elif (
                current is not None
                and not current.unknown
                and current.shape == "round"
                and abs(float(current.diameter_mm or 0) - data["diameter_mm"]) <= 0.05
                and current.key_width_mm
                and current.key_span_mm
            ):
                data["key_width_mm"] = float(current.key_width_mm)
                data["key_width_plus_mm"] = float(current.key_width_plus_mm or 0)
                data["key_span_mm"] = float(current.key_span_mm)
                data["key_span_plus_mm"] = float(current.key_span_plus_mm or 0)
                data["key_side"] = current.key_side
                data["key_optional"] = bool(current.key_optional)
        nominal_changed = (
            current is None
            or current.unknown
            or current.shape != shape
            or (shape == "rect" and (
                abs(float(current.width_mm or 0) - float(data.get("width_mm") or 0)) > 0.05
                or abs(float(current.height_mm or 0) - float(data.get("height_mm") or 0)) > 0.05
            ))
            or (shape != "rect" and abs(float(current.diameter_mm or 0) - float(data.get("diameter_mm") or 0)) > 0.05)
        )
        kept_unverified_pitch = False
        if current is not None and not current.unknown and not nominal_changed:
            for key in (
                "min_center_a_mm",
                "min_center_b_mm",
                "min_center_x_mm",
                "min_center_y_mm",
                "centers_interchangeable",
            ):
                data[key] = getattr(current, key)
            pitches = (
                data.get("min_center_a_mm"),
                data.get("min_center_b_mm"),
                data.get("min_center_x_mm"),
                data.get("min_center_y_mm"),
            )
            if not current.verified and (any(pitches) or data.get("centers_interchangeable")):
                kept_unverified_pitch = True
        if current is not None and not current.unknown:
            changed = changed or abs(float(current.clearance_mm or 0) - float(data.get("clearance_mm") or 0)) > 0.05
            changed = changed or abs(float(current.depth_behind_mm or 0) - float(data.get("depth_behind_mm") or 0)) > 0.05
            changed = changed or _holes_text(current) != holes.text().strip()
        if not changed:
            return
        data["source"] = ""
        data["note"] = _SHOP_NOTE
        # A tolerance edit keeps the IEC 30/50 pitches. Those were not checked
        # on an official publication, so the opening stays unverified.
        data["verified"] = not kept_unverified_pitch
        try:
            parse_cutout(data)
        except ValueError:
            return
        self._session.part_set_cutout(ref, data, include_key=False if shape == "rect" else None)
        if self._on_component_change:
            self._on_component_change()
        self._refresh_cutout_editor(ref)

    def _spacing_spin(self, value: float, tip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1000.0)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        spin.setSuffix(" mm")
        spin.setSpecialValueText("—")
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)
        spin.setToolTip(tip)
        return spin

    def _refresh_cutout_editor(self, ref: str) -> None:
        """Rebuild after the signal returns, so the controls match the stored opening."""

        session = self._session
        if session is None:
            return
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.show_component(session, ref) if self._session is session else None)

    def _commit_include_key(self, ref: str, include: bool) -> None:
        if self._session is None:
            return
        self._session.part_set_cutout(ref, include_key=include)
        if self._on_component_change:
            self._on_component_change()
        self._refresh_cutout_editor(ref)

    def _commit_cutout_unknown(self, ref: str, unknown: bool) -> None:
        if self._session is None:
            return
        if unknown:
            self._session.part_set_cutout(ref, unknown=True)
        else:
            self._session.part_set_cutout(ref, unknown=False)
        if self._on_component_change:
            self._on_component_change()
        self._refresh_cutout_editor(ref)

    def _commit_spacing(self, ref: str, horizontal: float, vertical: float, swapped: bool) -> None:
        if self._session is None or horizontal <= 0 or vertical <= 0:
            return
        self._session.part_set_cutout(
            ref,
            min_center_x_mm=horizontal,
            min_center_y_mm=vertical,
            centers_interchangeable=swapped,
        )
        if self._on_component_change:
            self._on_component_change()

    def _list_ref(self, item: QListWidgetItem | None) -> str | None:
        if item is None:
            return None
        ref = item.data(Qt.ItemDataRole.UserRole)
        if ref:
            return str(ref)
        text = item.text().split()
        return text[0] if text else None

    def _list_clicked(self, item: QListWidgetItem) -> None:
        ref = self._list_ref(item)
        if ref:
            self.select_requested.emit(ref)

    def _list_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        ref = self._list_ref(item)
        if not ref:
            return
        self.select_requested.emit(ref)
        menu = QMenu(self)
        menu.addAction("Select on sheet", lambda r=ref: self.select_requested.emit(r))
        menu.addAction("Object editor…", lambda r=ref: self.edit_requested.emit(r))
        menu.addAction("Catalog part…", lambda r=ref: self.catalog_requested.emit(r))
        menu.addAction("Confirm envelope…", lambda r=ref: self.envelope_requested.emit(r))
        menu.addAction("Set address…", lambda r=ref: self.address_requested.emit(r))
        menu.addSeparator()
        menu.addAction("Delete", lambda r=ref: self.delete_requested.emit(r))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _commit_scale(self, ref: str, value: float) -> None:
        if self._session is None:
            return
        self._session.set_scale(ref, value)
        if self._on_component_change:
            self._on_component_change()

    def _commit_pins_locked(self, ref: str, locked: bool) -> None:
        if self._session is None:
            return
        self._session.set_pins_locked(ref, locked)
        if self._on_component_change:
            self._on_component_change()

    def _commit_address(self, ref: str, ip: str | None = None, node: str | None = None) -> None:
        if self._session is None:
            return
        self._session.set_address(ref, ip=ip, node=node)
        if self._on_component_change:
            self._on_component_change()

    def _set_form(self, rows: list[tuple[str, str]]) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        for k, v in rows:
            label = QLabel(v)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            self.form.addRow(k, label)

    def _set_edits(self, rows: list[tuple[str, str, str]]) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        for label, value, field in rows:
            edit = QLineEdit(value or "")
            if field == "rev" and not (value or "").strip():
                edit.setPlaceholderText("A")
            edit.editingFinished.connect(lambda f=field, e=edit: self._commit(f, e.text()))
            self.form.addRow(label, edit)

    def _commit(self, field: str, value: str, edit: QLineEdit | None = None) -> None:
        if self._session is None:
            return
        if field == "control_voltage":
            try:
                self._session.set_control_voltage(value)
            except ValueError:
                if edit is not None:
                    sheet = self._session.sheet()
                    if sheet.kind in {"schematic", "io"} and sheet.control_voltage is not None:
                        shown = sheet.control_voltage
                    else:
                        shown = self._session.project.control_voltage
                    edit.setText("" if shown is None else f"{float(shown):g}")
                return
            if self._on_change:
                self._on_change()
            return
        self._session.set_title_block(**{field: value})
        if self._on_change:
            self._on_change()

    def _add_numbering_combo(self, settings: dict) -> None:
        box = QComboBox()
        labels = {
            "sheet_line": "sheet/line (101…)",
            "sequential": "sequential (1, 2…)",
            "iec": "IEC (1-1)",
        }
        keys = ["sheet_line", "sequential", "iec"]
        box.addItems([labels[k] for k in keys])
        current = settings.get("scheme") or "sheet_line"
        box.blockSignals(True)
        box.setCurrentIndex(keys.index(current) if current in keys else 0)
        box.blockSignals(False)
        box.currentIndexChanged.connect(lambda i: self._commit_numbering(scheme=keys[i]))
        self.form.addRow("Wire numbers", box)
        auto = QCheckBox("Number wires as drawn")
        auto.blockSignals(True)
        auto.setChecked(bool(settings.get("enabled", True)))
        auto.blockSignals(False)
        auto.toggled.connect(lambda on: self._commit_numbering(enabled=on))
        self.form.addRow("", auto)
        split = QCheckBox("Split feed-through numbers")
        split.setToolTip("Each side of a feed-through terminal gets its own wire number.")
        split.blockSignals(True)
        split.setChecked(bool(settings.get("split_feedthrough")))
        split.blockSignals(False)
        split.toggled.connect(lambda on: self._commit_numbering(split_feedthrough=on))
        self.form.addRow("", split)

    def _commit_numbering(
        self,
        scheme: str | None = None,
        enabled: bool | None = None,
        split_feedthrough: bool | None = None,
    ) -> None:
        if self._session is None:
            return
        self._session.set_wire_numbering(
            scheme=scheme,
            enabled=enabled,
            split_feedthrough=split_feedthrough,
        )
        if self._on_change:
            self._on_change()

    def _add_xref_combo(self, settings: dict) -> None:
        box = QComboBox()
        keys = ["underline", "bar", "parentheses"]
        labels = {
            "underline": "underline (NC)",
            "bar": "bar over NC",
            "parentheses": "(parentheses) NC",
        }
        box.addItems([labels[k] for k in keys])
        current = settings.get("nc_mark") or "underline"
        box.blockSignals(True)
        box.setCurrentIndex(keys.index(current) if current in keys else 0)
        box.blockSignals(False)
        box.currentIndexChanged.connect(lambda i: self._commit_xref(nc_mark=keys[i]))
        self.form.addRow("NC mark", box)
        spin = QDoubleSpinBox()
        spin.setRange(2.5, 50.0)
        spin.setSingleStep(0.1)
        spin.setDecimals(2)
        spin.setSuffix(" mm")
        spin.blockSignals(True)
        spin.setValue(float(settings.get("rung_pitch_mm") or 12.7))
        spin.blockSignals(False)
        spin.setToolTip("NEMA/JIC line pitch. 12.7 mm is 0.5 in. Line = sheet×100 + rung.")
        spin.valueChanged.connect(lambda v: self._commit_xref(rung_pitch_mm=float(v)))
        self.form.addRow("Line pitch", spin)

    def _commit_xref(
        self,
        nc_mark: str | None = None,
        rung_pitch_mm: float | None = None,
    ) -> None:
        if self._session is None:
            return
        self._session.set_cross_reference(nc_mark=nc_mark, rung_pitch_mm=rung_pitch_mm)
        if self._on_change:
            self._on_change()

    def _add_pref_combo(self, label: str, items: list[str], current: str, field: str) -> None:
        box = QComboBox()
        box.addItems(items)
        want = current if current in items else items[0]
        box.blockSignals(True)
        box.setCurrentText(want)
        box.blockSignals(False)
        box.currentTextChanged.connect(lambda v, f=field: self._commit(f, v))
        self.form.addRow(label, box)

    def _add_phase_row(self) -> None:
        button = QPushButton("Phase colors…")
        button.setToolTip(
            "L1, L2, and L3 strokes on 3-phase power sheets. "
            "Black/red/blue and brown/orange/yellow are a US shop convention. "
            "Brown, black, and grey are a permitted IEC default, not a required order. "
            "IEC neutral: blue (default light blue). IEC 60445 §6.2.2 is unverified "
            "from an official source. Control wiring stays the ordinary ink."
        )
        button.clicked.connect(self._edit_phase_colors)
        self.form.addRow("Phase colors", button)

    def _edit_phase_colors(self) -> None:
        if self._session is None:
            return
        from PySide6.QtWidgets import QDialog, QMessageBox

        from panelcad.gui.phase_dialog import PhaseColorDialog

        dialog = PhaseColorDialog(self._session.phase_colors(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            payload = self._session.set_phase_colors(**dialog.values())
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Phase colors", str(exc))
            return
        if self._on_change:
            self._on_change()
        warnings = [str(item) for item in (payload.get("warnings") or []) if str(item).strip()]
        if warnings:
            QMessageBox.warning(self, "Phase colors", "\n".join(warnings))

    def _add_logo_row(self, logo: str) -> None:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        path_label = QLabel(logo.split("/")[-1].split("\\")[-1] if logo else "(none)")
        path_label.setToolTip(logo)
        box.addWidget(path_label, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._on_logo() if self._on_logo else None)
        box.addWidget(browse)
        if logo:
            clear = QPushButton("Clear")
            clear.clicked.connect(lambda: self._on_clear_logo() if self._on_clear_logo else None)
            box.addWidget(clear)
        self.form.addRow("Logo", row)
