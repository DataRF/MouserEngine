"""Diálogos: configuración, importación de BOM (mapeo de columnas) y búsqueda en Mouser."""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..bom import FIELD_LABELS, FIELDS, BomError, BomTable, build_items, set_header_row, switch_sheet
from ..config import ENV_API_KEY, ENV_CART_API_KEY, Settings, config_dir
from ..formatting import fmt_int, fmt_money, fmt_price
from ..models import BomItem, Part, packaging_label
from ..mouser_api import DAILY_LIMIT, MouserClient
from ..passives import RECOGNIZED_MANUFACTURERS
from ..pricing import price_for_qty, purchase_qty
from ..quote import rank_options
from .workers import Worker, WorkerPool


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --- Configuración -------------------------------------------------------------

class SettingsDialog(QDialog):
    """Configuración en pestañas: claves de API, consulta, pasivos sin MPN y empresa."""

    def __init__(self, settings: Settings, client_factory: Callable[[str], MouserClient],
                 workers: WorkerPool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración")
        self.setMinimumWidth(600)
        self.settings = settings
        self.client_factory = client_factory
        self.workers = workers

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_api_tab(), "API de Mouser")
        self.tabs.addTab(self._build_query_tab(), "Consulta")
        self.tabs.addTab(self._build_passives_tab(), "Pasivos sin MPN")
        self.tabs.addTab(self._build_company_tab(), "Empresa")
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Guardar")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --- pestañas ----------------------------------------------------------------

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _key_row(edit: QLineEdit) -> QHBoxLayout:
        row = QHBoxLayout()
        edit.setEchoMode(QLineEdit.Password)
        edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        show = QCheckBox("Mostrar")
        show.toggled.connect(lambda on: edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(QLabel("API key:"))
        row.addWidget(edit, 1)
        row.addWidget(show)
        return row

    def _build_api_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        search = QGroupBox("Search API · precios y stock")
        search_layout = QVBoxLayout(search)
        search_layout.addWidget(self._hint(
            "Clave de la <b>Search API</b> (My Mouser → APIs → «Buscar API»). Se guarda solo en este "
            f"computador, en:<br><code>{config_dir() / 'config.json'}</code>"))
        self.key_edit = QLineEdit(self.settings.api_key)
        search_layout.addLayout(self._key_row(self.key_edit))
        if self.settings.api_key_from_env:
            search_layout.addWidget(self._hint(
                f"Se está usando la variable de entorno {ENV_API_KEY}, que tiene prioridad."))
        test_row = QHBoxLayout()
        self.test_button = QPushButton("Probar conexión")
        self.test_button.clicked.connect(self._test)
        test_row.addWidget(self.test_button)
        test_row.addStretch(1)
        search_layout.addLayout(test_row)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        self.test_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.test_label.setMinimumHeight(self.test_label.fontMetrics().lineSpacing() * 4 + 6)
        self.test_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        search_layout.addWidget(self.test_label)
        search_layout.addWidget(self._hint(
            f"Consultas realizadas hoy: {fmt_int(self.settings.calls_today)} de {fmt_int(DAILY_LIMIT)} "
            "(límite diario de Mouser)."))
        layout.addWidget(search)

        cart = QGroupBox("Cart API · crear el carro en Mouser")
        cart_layout = QVBoxLayout(cart)
        cart_layout.addWidget(self._hint(
            "Clave de <b>Cart/Order API</b> (My Mouser → APIs). Mouser la autoriza por separado: "
            "mientras aparezca como «Pendiente», crear el carro no funcionará. La aplicación solo crea "
            "el carro; nunca emite pedidos."))
        self.cart_key_edit = QLineEdit(self.settings.cart_api_key)
        cart_layout.addLayout(self._key_row(self.cart_key_edit))
        if os.environ.get(ENV_CART_API_KEY, "").strip():
            cart_layout.addWidget(self._hint(
                f"Se está usando la variable de entorno {ENV_CART_API_KEY}, que tiene prioridad."))
        layout.addWidget(cart)
        layout.addStretch(1)
        return page

    def _build_query_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.batch = QSpinBox()
        self.batch.setRange(1, 10)
        self.batch.setValue(self.settings.batch_size)
        self.batch.setToolTip("Partes por consulta (Mouser permite hasta 10). Menos = más consultas.")
        form.addRow("Partes por consulta:", self.batch)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 120)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(self.settings.timeout)
        form.addRow("Tiempo máximo de espera:", self.timeout)
        self.auto_refresh = QSpinBox()
        self.auto_refresh.setRange(0, 1440)
        self.auto_refresh.setSuffix(" min")
        self.auto_refresh.setSpecialValueText("Desactivada")
        self.auto_refresh.setValue(self.settings.auto_refresh_minutes)
        form.addRow("Actualización automática:", self.auto_refresh)
        self.auto_query = QCheckBox("Consultar precios al abrir un BOM")
        self.auto_query.setChecked(self.settings.auto_query_on_load)
        form.addRow("", self.auto_query)
        self.fuzzy = QCheckBox("Buscar sugerencias para partes no encontradas")
        self.fuzzy.setChecked(self.settings.fuzzy_search)
        form.addRow("", self.fuzzy)
        return page

    def _build_passives_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.passives_check = QCheckBox("Elegir automáticamente resistencias y condensadores sin MPN")
        self.passives_check.setChecked(self.settings.passives_enabled)
        layout.addWidget(self.passives_check)
        layout.addWidget(self._hint(
            "La aplicación lee del BOM el valor, encapsulado, tolerancia, potencia, tensión y dieléctrico, "
            "busca en Mouser y elige la opción más conveniente que cumpla o supere cada especificación, "
            "prefiriendo fabricantes reconocidos. Cuando el BOM no indica un dato se usan estos valores:"))
        form = QFormLayout()
        self.res_tol = QDoubleSpinBox()
        self.res_tol.setRange(0.01, 20)
        self.res_tol.setDecimals(2)
        self.res_tol.setSuffix(" %")
        self.res_tol.setValue(self.settings.res_tolerance_default)
        form.addRow("Tolerancia máxima de resistencias:", self.res_tol)
        self.cap_tol = QDoubleSpinBox()
        self.cap_tol.setRange(0.1, 80)
        self.cap_tol.setDecimals(1)
        self.cap_tol.setSuffix(" %")
        self.cap_tol.setValue(self.settings.cap_tolerance_default)
        form.addRow("Tolerancia máxima de condensadores:", self.cap_tol)
        self.cap_volt = QDoubleSpinBox()
        self.cap_volt.setRange(2.5, 1000)
        self.cap_volt.setDecimals(1)
        self.cap_volt.setSuffix(" V")
        self.cap_volt.setValue(self.settings.cap_voltage_default)
        form.addRow("Tensión mínima de condensadores:", self.cap_volt)
        layout.addLayout(form)
        layout.addWidget(self._hint(
            "Fabricantes reconocidos: " + ", ".join(RECOGNIZED_MANUFACTURERS) + "."))
        layout.addStretch(1)
        return page

    def _build_company_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.company_edit = QLineEdit(self.settings.company_name)
        form.addRow("Razón social:", self.company_edit)
        form.addRow("", self._hint("Se usa en el Excel exportado y en la orden de compra interna."))
        return page

    # --- acciones -----------------------------------------------------------------

    def _fit_height(self) -> None:
        layout = self.layout()
        layout.activate()
        needed = layout.totalSizeHint().height()
        if needed > self.height():
            self.resize(self.width(), needed)

    def _test(self) -> None:
        key = self.key_edit.text().strip() or self.settings.effective_api_key
        if not key:
            self.test_label.setText("Ingrese la API key.")
            return
        self.test_button.setEnabled(False)
        self.test_label.setText("Probando…")
        client = self.client_factory(key)
        worker = Worker(client.test_connection)
        worker.signals.result.connect(self._test_ok)
        worker.signals.error.connect(self._test_failed)
        worker.signals.finished.connect(lambda: self.test_button.setEnabled(True))
        self.workers.start(worker)

    def _test_ok(self, message: str) -> None:
        self.test_label.setText(f"<span style='color:#1A7F37'>✔ {html.escape(message)}</span>")
        self._fit_height()

    def _test_failed(self, exc: Exception) -> None:
        self.test_label.setText(f"<span style='color:#CF222E'>✖ {html.escape(str(exc))}</span>")
        self._fit_height()

    def apply(self) -> None:
        self.settings.api_key = self.key_edit.text().strip()
        self.settings.cart_api_key = self.cart_key_edit.text().strip()
        self.settings.batch_size = self.batch.value()
        self.settings.timeout = self.timeout.value()
        self.settings.auto_refresh_minutes = self.auto_refresh.value()
        self.settings.auto_query_on_load = self.auto_query.isChecked()
        self.settings.fuzzy_search = self.fuzzy.isChecked()
        self.settings.passives_enabled = self.passives_check.isChecked()
        self.settings.res_tolerance_default = self.res_tol.value()
        self.settings.cap_tolerance_default = self.cap_tol.value()
        self.settings.cap_voltage_default = self.cap_volt.value()
        self.settings.company_name = self.company_edit.text().strip()


# --- Importación de BOM ----------------------------------------------------------

class ImportDialog(QDialog):
    """Muestra la vista previa del archivo y permite corregir las columnas detectadas."""

    PREVIEW_ROWS = 25

    def __init__(self, table: BomTable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Importar BOM")
        self.resize(1000, 640)
        self.table = table
        self._updating = False

        layout = QVBoxLayout(self)
        title = QLabel(f"<b>{Path(table.path).name}</b>")
        layout.addWidget(title)
        hint = QLabel("Revise que cada dato corresponda a la columna correcta. "
                      "Se detectaron automáticamente; puede cambiarlos si es necesario.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        top = QHBoxLayout()
        self.sheet_combo = QComboBox()
        self.sheet_combo.addItems(table.sheet_names)
        self.sheet_combo.setCurrentText(table.sheet)
        self.sheet_combo.currentTextChanged.connect(self._sheet_changed)
        self.sheet_combo.setVisible(len(table.sheet_names) > 1)
        sheet_label = QLabel("Hoja:")
        sheet_label.setVisible(len(table.sheet_names) > 1)
        top.addWidget(sheet_label)
        top.addWidget(self.sheet_combo)
        top.addSpacing(16)
        top.addWidget(QLabel("Fila de encabezados:"))
        self.header_spin = QSpinBox()
        self.header_spin.setRange(1, max(1, len(table.grid)))
        self.header_spin.setValue(table.header_row + 1)
        self.header_spin.valueChanged.connect(self._header_changed)
        top.addWidget(self.header_spin)
        top.addStretch(1)
        layout.addLayout(top)

        mapping_box = QGroupBox("Columnas")
        grid = QGridLayout(mapping_box)
        self.combos: dict[str, QComboBox] = {}
        for index, name in enumerate(FIELDS):
            label = QLabel(FIELD_LABELS[name] + ":")
            combo = QComboBox()
            combo.setMinimumWidth(200)
            combo.currentIndexChanged.connect(self._mapping_changed)
            self.combos[name] = combo
            grid.addWidget(label, index // 2, (index % 2) * 2)
            grid.addWidget(combo, index // 2, (index % 2) * 2 + 1)
        layout.addWidget(mapping_box)

        self.preview = QTableWidget()
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.setAlternatingRowColors(True)
        self.preview.verticalHeader().setDefaultSectionSize(22)
        layout.addWidget(self.preview, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Importar")
        self.ok_button.setObjectName("Primary")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._updating = True
        headers = self.table.headers
        count = self.table.column_count
        for name, combo in self.combos.items():
            combo.clear()
            combo.addItem("(ninguna)", -1)
            for col in range(count):
                text = headers[col] if col < len(headers) else ""
                combo.addItem(f"{_column_letter(col)}: {text}".strip(), col)
            idx = self.table.mapping.get(name)
            combo.setCurrentIndex(0 if idx is None else idx + 1)
        self._updating = False
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        table = self.table
        count = table.column_count
        rows = table.grid[table.header_row + 1: table.header_row + 1 + self.PREVIEW_ROWS]
        self.preview.clear()
        self.preview.setColumnCount(count)
        self.preview.setRowCount(len(rows))
        mapped = {v: k for k, v in table.mapping.items()}
        headers = []
        for col in range(count):
            original = table.headers[col] if col < len(table.headers) else ""
            label = f"{_column_letter(col)}: {original}"
            if col in mapped:
                label += f"\n→ {FIELD_LABELS[mapped[col]]}"
            headers.append(label)
        self.preview.setHorizontalHeaderLabels(headers)
        for r, row in enumerate(rows):
            for c in range(count):
                item = QTableWidgetItem(row[c] if c < len(row) else "")
                if c in mapped:
                    item.setBackground(QColor("#EAF2FC"))
                self.preview.setItem(r, c, item)
        self.preview.setVerticalHeaderLabels([str(table.header_row + 2 + i) for i in range(len(rows))])
        self.preview.resizeColumnsToContents()
        for col in range(count):
            self.preview.setColumnWidth(col, min(max(self.preview.columnWidth(col), 90), 260))
        items, warnings = build_items(table)
        included = sum(1 for i in items if i.include)
        text = (f"Se leerán <b>{len(items)}</b> partes distintas ({included} con cantidad mayor a 0) "
                f"desde {sum(len(i.rows) for i in items)} filas del BOM.")
        if warnings:
            text += "<br><span style='color:#9A6700'>" + "<br>".join(warnings) + "</span>"
        self.summary_label.setText(text)
        self.ok_button.setEnabled(bool(items))

    def _sheet_changed(self, name: str) -> None:
        if self._updating or not name or name == self.table.sheet:
            return
        try:
            self.table = switch_sheet(self.table, name)
        except BomError as exc:
            self.summary_label.setText(str(exc))
            return
        self.header_spin.blockSignals(True)
        self.header_spin.setRange(1, max(1, len(self.table.grid)))
        self.header_spin.setValue(self.table.header_row + 1)
        self.header_spin.blockSignals(False)
        self._refresh_all()

    def _header_changed(self, value: int) -> None:
        set_header_row(self.table, value - 1)
        self._refresh_all()

    def _mapping_changed(self) -> None:
        if self._updating:
            return
        mapping = {}
        for name, combo in self.combos.items():
            col = combo.currentData()
            if col is not None and col >= 0:
                mapping[name] = col
        self.table.mapping = mapping
        self._refresh_preview()


# --- Búsqueda en Mouser -----------------------------------------------------------

class SearchDialog(QDialog):
    """Búsqueda por palabra clave para asignar o reemplazar la parte de una línea."""

    COLUMNS = ["N° Mouser", "MPN", "Fabricante", "Descripción", "Empaque", "Stock", "Mín / Múlt",
               "Precio unit.", "Total", "Ciclo de vida"]

    def __init__(self, client: MouserClient, workers: WorkerPool, item: BomItem, required: int,
                 keyword: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Buscar en Mouser")
        self.resize(1100, 560)
        self.client = client
        self.workers = workers
        self.item = item
        self.required = max(1, required)
        self.parts: list[Part] = []
        self.selected_part: Part | None = None
        self._worker: Worker | None = None

        layout = QVBoxLayout(self)
        context = QLabel(
            f"Línea {item.rows_label} · {item.designators or 'sin designadores'} · "
            f"{item.display_description or item.mpn or ''} · se necesitan {fmt_int(self.required)} unidades")
        context.setObjectName("Hint")
        context.setWordWrap(True)
        layout.addWidget(context)

        row = QHBoxLayout()
        self.keyword = QLineEdit(keyword)
        self.keyword.setPlaceholderText("Número de parte o palabras clave, p. ej. «10k 0603 1%»")
        self.keyword.returnPressed.connect(self.search)
        self.in_stock = QCheckBox("Solo con stock")
        self.search_button = QPushButton("Buscar")
        self.search_button.setObjectName("Primary")
        self.search_button.clicked.connect(self.search)
        row.addWidget(self.keyword, 1)
        row.addWidget(self.in_stock)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._accept_selected)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.open_button = buttons.addButton("Ver en Mouser", QDialogButtonBox.ActionRole)
        self.open_button.clicked.connect(self._open_selected)
        self.use_button = buttons.addButton("Usar esta parte", QDialogButtonBox.AcceptRole)
        self.use_button.setObjectName("Primary")
        cancel = buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        self.use_button.clicked.connect(self._accept_selected)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._selection_changed()
        if keyword.strip():
            self.search()

    def search(self) -> None:
        text = self.keyword.text().strip()
        if not text:
            return
        if self._worker is not None:
            self._worker.cancel()
        self.search_button.setEnabled(False)
        self.status.setText("Buscando en Mouser…")
        worker = Worker(self.client.search_keyword, text, 50, self.in_stock.isChecked(), with_cancel=True)
        worker.signals.result.connect(self._show_results)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self.search_button.setEnabled(True))
        self._worker = worker
        self.workers.start(worker)

    def _show_results(self, result) -> None:
        total, parts = result
        self.parts = rank_options(parts, self.required)
        self.table.setRowCount(len(self.parts))
        for r, part in enumerate(self.parts):
            qty = purchase_qty(self.required, part.min_qty, part.mult)
            option = price_for_qty(part.price_breaks, qty)
            values = [
                part.mouser_pn, part.mpn, part.manufacturer, part.description,
                packaging_label(part.packaging),
                fmt_int(part.stock) if part.stock is not None else "—",
                "1" if part.min_qty == 1 and part.mult == 1 else f"{fmt_int(part.min_qty)} / {fmt_int(part.mult)}",
                fmt_price(option.unit_price) if option else "",
                fmt_money(option.ext_price, part.currency) if option else "",
                part.lifecycle_label,
            ]
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if c in (5, 6, 7, 8):
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 5 and part.stock is not None and part.stock < qty:
                    cell.setForeground(QColor("#CF222E"))
                self.table.setItem(r, c, cell)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        shown = len(self.parts)
        self.status.setText(f"{fmt_int(total)} resultados en Mouser; se muestran {shown}, ordenados por "
                            f"conveniencia para {fmt_int(self.required)} unidades.")
        if shown:
            self.table.selectRow(0)

    def _show_error(self, exc: Exception) -> None:
        self.status.setText(f"<span style='color:#CF222E'>{exc}</span>")

    def _current(self) -> Part | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        return self.parts[row] if 0 <= row < len(self.parts) else None

    def _selection_changed(self) -> None:
        part = self._current()
        self.use_button.setEnabled(part is not None and part.orderable)
        self.open_button.setEnabled(part is not None and bool(part.product_url))

    def _open_selected(self) -> None:
        part = self._current()
        if part and part.product_url:
            QDesktopServices.openUrl(QUrl(part.product_url))

    def _accept_selected(self, *args) -> None:
        part = self._current()
        if part is None or not part.orderable:
            return
        self.selected_part = part
        self.accept()

    def reject(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        super().reject()
