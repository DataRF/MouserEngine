"""Pestaña «Escenarios de volumen»: barra de cantidad, gráfico y tabla comparativa."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import DEFAULT_SCENARIOS, parse_quantities
from ..formatting import fmt_board_cost, fmt_int, fmt_money, fmt_num
from ..models import BomItem, QuoteParams
from ..scenarios import CostModel, ScenarioRow, scenario_table
from .scenario_chart import ScenarioChart
from .workers import Worker, WorkerPool

SLIDER_STEPS = 1000
BIG_DROP = -10  # % de baja del costo por placa que se destaca como descuento importante


@dataclass
class _Computed:
    generation: int
    rows: list[ScenarioRow]
    model: CostModel
    curve: list
    currency: str


def _compute(generation: int, items: list[BomItem], params: QuoteParams, quantities: list[int],
             max_boards: int) -> _Computed:
    rows = scenario_table(items, params, quantities)
    model = CostModel(items, params)
    curve = model.curve(max_boards, extra=quantities + [params.boards])
    currency = next((r.currency for r in rows if r.currency), "") or model.currency
    return _Computed(generation, rows, model, curve, currency)


class ScenariosPanel(QWidget):
    use_quantity = Signal(int)
    settings_changed = Signal(str, object)  # (nombre del ajuste, valor)

    COLUMNS = ["Placas", "Costo\npor placa", "Variación\npor placa", "Costo\ntotal", "Solo\ncomponentes",
               "Ahorro posible\npor tramos", "Partes sin\nstock suficiente", "Partes\nsin precio"]
    COLUMN_TIPS = [
        "Cantidad de placas o equipos a fabricar",
        "Costo total dividido por la cantidad de placas (incluye flete, arancel e IVA si se ingresaron)",
        "Cambio del costo por placa respecto de la fila anterior",
        "Componentes más flete, arancel e IVA",
        "Solo el precio de los componentes en Mouser",
        "Lo que se ahorraría comprando hasta el siguiente tramo de precio cuando sale más barato "
        "(se aplica con «Optimizar por tramos de precio»)",
        "Partes cuyo stock en Mouser no alcanza para esa cantidad",
        "Partes sin precio en Mouser: no se suman al costo",
    ]

    def __init__(self, workers: WorkerPool, quantities: str = DEFAULT_SCENARIOS, max_boards: int = 1000,
                 mode: str = "overlay", parent=None):
        super().__init__(parent)
        self.workers = workers
        self._generation = 0
        self._inputs: tuple | None = None
        self._model: CostModel | None = None
        self._rows: list[ScenarioRow] = []
        self._current_boards = 1
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._start)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Cantidades a comparar:"))
        self.quantities_edit = QLineEdit(quantities)
        self.quantities_edit.setToolTip("Cantidades de placas separadas por coma. Se guardan para las próximas cotizaciones.")
        self.quantities_edit.editingFinished.connect(self._quantities_edited)
        top.addWidget(self.quantities_edit, 1)
        reset = QPushButton("Restablecer")
        reset.setToolTip(f"Volver a {DEFAULT_SCENARIOS}")
        reset.clicked.connect(self._reset_quantities)
        top.addWidget(reset)
        top.addSpacing(16)
        top.addWidget(QLabel("Vista:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Superpuesta (dos ejes)", "overlay")
        self.mode_combo.addItem("Separada (dos gráficos)", "split")
        self.mode_combo.setCurrentIndex(0 if mode != "split" else 1)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        top.addWidget(self.mode_combo)
        layout.addLayout(top)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Cantidad:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.setToolTip("Mueva la barra para ver el costo en cada cantidad (escala logarítmica)")
        self.slider.valueChanged.connect(self._slider_moved)
        slider_row.addWidget(self.slider, 1)
        self.boards_spin = QSpinBox()
        self.boards_spin.setRange(1, max_boards)
        self.boards_spin.setGroupSeparatorShown(True)
        self.boards_spin.setSuffix(" placas")
        self.boards_spin.setMinimumWidth(110)
        self.boards_spin.valueChanged.connect(self._spin_changed)
        slider_row.addWidget(self.boards_spin)
        slider_row.addSpacing(12)
        slider_row.addWidget(QLabel("Máximo:"))
        self.max_spin = QSpinBox()
        self.max_spin.setRange(10, 1_000_000)
        self.max_spin.setGroupSeparatorShown(True)
        self.max_spin.setValue(max_boards)
        self.max_spin.setMinimumWidth(90)
        self.max_spin.editingFinished.connect(self._max_changed)
        slider_row.addWidget(self.max_spin)
        self.use_button = QPushButton("Usar en la cotización")
        self.use_button.setToolTip("Lleva esta cantidad al campo «Placas / equipos» de la cotización")
        self.use_button.clicked.connect(lambda: self.use_quantity.emit(self.boards_spin.value()))
        slider_row.addWidget(self.use_button)
        layout.addLayout(slider_row)

        self.chart = ScenarioChart()
        self.chart.set_mode(self.mode_combo.currentData())
        self.chart.picked.connect(self.set_boards)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        for column, tip in enumerate(self.COLUMN_TIPS):
            self.table.horizontalHeaderItem(column).setToolTip(tip)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._row_selected)

        self.note = QLabel("")
        self.note.setObjectName("Hint")
        self.note.setWordWrap(True)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.table, 1)
        bottom_layout.addWidget(self.note)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.chart)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 260])
        splitter.splitterMoved.connect(self._splitter_moved)
        layout.addWidget(splitter, 1)
        self.splitter = splitter
        self._user_split = False  # si el usuario movió el divisor, se respeta su altura
        self.set_boards(1)

    # --- API ------------------------------------------------------------------------

    @property
    def quantities(self) -> list[int]:
        return parse_quantities(self.quantities_edit.text()) or parse_quantities(DEFAULT_SCENARIOS)

    @property
    def max_boards(self) -> int:
        return self.max_spin.value()

    def refresh(self, items: list[BomItem], params: QuoteParams, current_boards: int) -> None:
        """Programa el recálculo (se agrupan cambios seguidos)."""
        self._inputs = (items, params)
        self._current_boards = current_boards
        self.chart.set_stale(bool(self.chart.points))
        self._timer.start()

    def set_boards(self, boards: int) -> None:
        boards = max(1, min(self.max_boards, int(boards)))
        self.boards_spin.blockSignals(True)
        self.boards_spin.setValue(boards)
        self.boards_spin.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(boards))
        self.slider.blockSignals(False)
        self._update_cursor(boards)

    # --- cálculo en segundo plano ---------------------------------------------------------

    def _start(self) -> None:
        if self._inputs is None:
            return
        items, params = self._inputs
        self._generation += 1
        worker = Worker(_compute, self._generation, list(items), params, self.quantities, self.max_boards)
        worker.signals.result.connect(self._computed)
        self.workers.start(worker)

    def _computed(self, result: _Computed) -> None:
        if result.generation != self._generation:
            return  # llegó un resultado viejo: se descarta
        self._model = result.model
        self._rows = result.rows
        self.chart.set_data(result.curve, result.currency, self.quantities, self.max_boards)
        self._update_cursor(self.boards_spin.value())
        self._fill_table(result.rows, result.currency)

    def _update_cursor(self, boards: int) -> None:
        point = self._model.point(boards) if self._model is not None else None
        self.chart.set_cursor(boards, point)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_splitter()

    def _splitter_moved(self, *_) -> None:
        self._user_split = True

    def _fit_splitter(self) -> None:
        """Da a la tabla la altura justa para mostrar todas las cantidades; el resto es del gráfico."""
        if self._user_split:
            return
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        header = self.table.horizontalHeader().sizeHint().height()
        rows = self.table.verticalHeader().length() or 3 * self.table.verticalHeader().defaultSectionSize()
        wanted = header + rows + 2 * self.table.frameWidth() + 2
        if self.note.text():
            wanted += self.note.heightForWidth(max(200, self.width() - 16)) + 6
        table = min(wanted, max(total // 2, total - 320))
        self.splitter.setSizes([total - table, table])

    # --- tabla ----------------------------------------------------------------------

    def _fill_table(self, rows: list[ScenarioRow], currency: str) -> None:
        self.table.setRowCount(len(rows))
        bold = QFont()
        bold.setBold(True)
        for r, row in enumerate(rows):
            change = ""
            if row.change is not None:
                arrow = "▼" if row.change < 0 else ("▲" if row.change > 0 else "")
                change = f"{arrow} {fmt_num(row.change, 1)} %".strip()
            values = [
                fmt_int(row.boards),
                fmt_board_cost(row.total_unit, currency),
                change,
                fmt_money(row.total, currency),
                fmt_money(row.goods, currency),
                fmt_money(row.savings, currency) if row.savings else "—",
                fmt_int(row.short) if row.short else "—",
                fmt_int(row.unpriced) if row.unpriced else "—",
            ]
            big_drop = row.change is not None and row.change <= BIG_DROP
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 2 and big_drop:
                    cell.setForeground(QColor("#1A7F37"))
                    cell.setFont(bold)
                    cell.setToolTip("Descuento por volumen importante respecto de la cantidad anterior")
                if c in (6, 7) and value != "—":
                    cell.setForeground(QColor("#9A6700"))
                if row.boards == self._current_boards:
                    cell.setBackground(QColor("#EAF2FC"))
                    if c == 0:
                        cell.setToolTip("Cantidad usada en la cotización actual")
                self.table.setItem(r, c, cell)
            self.table.item(r, 0).setData(Qt.UserRole, row.boards)
        notes = []
        if rows and any(r.total != r.goods for r in rows):
            notes.append("El costo por placa y el total incluyen el flete, arancel e IVA ingresados.")
        if rows and rows[-1].unpriced:
            notes.append(f"{rows[-1].unpriced} partes sin precio no se incluyen (revíselas en la pestaña Partes).")
        notes.append("Seleccione una fila o haga clic en el gráfico para ver esa cantidad. "
                     "En verde, bajas de 10 % o más en el costo por placa.")
        self.note.setText(" ".join(notes))
        self._fit_splitter()

    def _row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            boards = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
            if boards:
                self.set_boards(int(boards))

    # --- controles ----------------------------------------------------------------------

    def _to_slider(self, boards: int) -> int:
        if self.max_boards <= 1:
            return 0
        return round(math.log10(max(1, boards)) / math.log10(self.max_boards) * SLIDER_STEPS)

    def _from_slider(self, position: int) -> int:
        if self.max_boards <= 1:
            return 1
        return max(1, min(self.max_boards, round(10 ** (position / SLIDER_STEPS * math.log10(self.max_boards)))))

    def _slider_moved(self, position: int) -> None:
        boards = self._from_slider(position)
        self.boards_spin.blockSignals(True)
        self.boards_spin.setValue(boards)
        self.boards_spin.blockSignals(False)
        self._update_cursor(boards)

    def _spin_changed(self, boards: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(boards))
        self.slider.blockSignals(False)
        self._update_cursor(boards)

    def _quantities_edited(self) -> None:
        values = parse_quantities(self.quantities_edit.text())
        text = ", ".join(str(v) for v in values) if values else DEFAULT_SCENARIOS
        if text != self.quantities_edit.text():
            self.quantities_edit.setText(text)
        self.settings_changed.emit("scenario_quantities", text)
        self._restart()

    def _reset_quantities(self) -> None:
        self.quantities_edit.setText(DEFAULT_SCENARIOS)
        self._quantities_edited()

    def _mode_changed(self) -> None:
        mode = self.mode_combo.currentData()
        self.chart.set_mode(mode)
        self.settings_changed.emit("chart_mode", mode)

    def _max_changed(self) -> None:
        value = self.max_spin.value()
        self.boards_spin.setRange(1, value)
        self.settings_changed.emit("scenario_max", value)
        self.set_boards(min(self.boards_spin.value(), value))
        self._restart()

    def _restart(self) -> None:
        if self._inputs is not None:
            items, params = self._inputs
            self.refresh(items, params, self._current_boards)
