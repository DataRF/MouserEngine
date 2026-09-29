"""Historial de cotizaciones: lista de cotizaciones guardadas y comparación con los precios actuales."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..formatting import fmt_int, fmt_money, fmt_num, fmt_price
from ..history import ComparisonRow, HistoryEntry, HistoryError, HistoryStore

UP_COLOR = "#CF222E"    # subió (peor para la compra)
DOWN_COLOR = "#1A7F37"  # bajó
WARN_COLOR = "#9A6700"


def fmt_when(value: datetime | None) -> str:
    return value.strftime("%d-%m-%Y %H:%M") if value else "—"


def _cell(text: str, right: bool = False, color: str | None = None, bold: bool = False,
          tip: str = "") -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    if right:
        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if color:
        cell.setForeground(QColor(color))
    if bold:
        font = QFont()
        font.setBold(True)
        cell.setFont(font)
    if tip:
        cell.setToolTip(tip)
    return cell


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(24)
    table.setWordWrap(False)
    return table


class HistoryDialog(QDialog):
    """Cotizaciones guardadas en este PC. Al cerrar con una acción, `action` indica cuál:
    "open" (abrir con los precios guardados), "compare" (abrir y comparar con los de hoy) o
    "export" (exportar a Excel sin reemplazar la cotización actual)."""

    COLUMNS = ["Fecha", "Motivo", "BOM", "Cliente", "Placas", "Total", "Partes", "Clave del carro"]

    def __init__(self, store: HistoryStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Historial de cotizaciones")
        self.resize(1000, 560)
        self.store = store
        self.entries: list[HistoryEntry] = []
        self.action: str | None = None
        self.selected: HistoryEntry | None = None

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Buscar por nombre del BOM, cliente o clave del carro…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.refresh)
        top.addWidget(self.filter_edit, 1)
        layout.addLayout(top)

        self.table = _table(self.COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda *_: self._finish("open"))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.empty = QLabel("")
        self.empty.setObjectName("Hint")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        hint = QLabel("Las cotizaciones se guardan con «Guardar en historial» (Ctrl+S) y automáticamente al "
                      f"exportar a Excel o crear el carro en Mouser. Solo en este computador: {store.path}")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.open_button = QPushButton("Abrir")
        self.open_button.setObjectName("Primary")
        self.open_button.setToolTip("Abre la cotización con los precios y el stock guardados")
        self.open_button.clicked.connect(lambda: self._finish("open"))
        self.compare_button = QPushButton("Comparar con precios actuales")
        self.compare_button.setToolTip("Abre la cotización, consulta Mouser y muestra qué cambió")
        self.compare_button.clicked.connect(lambda: self._finish("compare"))
        self.export_button = QPushButton("Exportar a Excel…")
        self.export_button.clicked.connect(lambda: self._finish("export"))
        self.delete_button = QPushButton("Eliminar")
        self.delete_button.clicked.connect(self._delete)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.reject)
        for button in (self.open_button, self.compare_button, self.export_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        try:
            self.entries = self.store.entries(self.filter_edit.text())
        except HistoryError as exc:
            self.entries = []
            self.empty.setText(str(exc))
        else:
            if self.entries:
                self.empty.setText("")
            elif self.filter_edit.text().strip():
                self.empty.setText("Ninguna cotización coincide con la búsqueda.")
            else:
                self.empty.setText("Todavía no hay cotizaciones guardadas.")
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            values = [
                _cell(fmt_when(e.created_at), tip=f"Precios consultados: {fmt_when(e.queried_at)}"),
                _cell(e.reason_label),
                _cell(e.bom_name or "—", tip=e.bom_path),
                _cell(e.client or "—"),
                _cell(fmt_int(e.boards), right=True),
                _cell(fmt_money(e.total, e.currency), right=True),
                _cell(f"{fmt_int(e.priced)} de {fmt_int(e.parts)}", right=True,
                      color=WARN_COLOR if e.unpriced else None,
                      tip=f"{e.unpriced} sin precio" if e.unpriced else ""),
                _cell(e.cart_key or "—"),
            ]
            for c, cell in enumerate(values):
                self.table.setItem(r, c, cell)
        if self.entries:
            self.table.selectRow(0)
        self._update_buttons()

    def _current(self) -> HistoryEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = rows[0].row()
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def _update_buttons(self) -> None:
        has = self._current() is not None
        for button in (self.open_button, self.compare_button, self.export_button, self.delete_button):
            button.setEnabled(has)

    def _finish(self, action: str) -> None:
        entry = self._current()
        if entry is None:
            return
        self.action, self.selected = action, entry
        self.accept()

    def _delete(self) -> None:
        entry = self._current()
        if entry is None:
            return
        answer = QMessageBox.question(
            self, "Eliminar del historial",
            f"¿Eliminar la cotización del {fmt_when(entry.created_at)} ({entry.bom_name or 'sin nombre'})?\n"
            "Esta acción no se puede deshacer.")
        if answer != QMessageBox.Yes:
            return
        try:
            self.store.delete(entry.id)
        except HistoryError as exc:
            QMessageBox.warning(self, "Historial", str(exc))
        self.refresh()


class ComparisonDialog(QDialog):
    """Qué cambió entre la cotización guardada y los precios actuales de Mouser."""

    COLUMNS = ["Parte", "N° Mouser", "Cant.", "Precio antes", "Precio ahora", "Total antes", "Total ahora",
               "Variación", "Stock ahora", "Estado"]

    def __init__(self, rows: list[ComparisonRow], before_total: Decimal, after_total: Decimal, currency: str,
                 saved_at: datetime | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparación con los precios actuales")
        self.resize(1180, 600)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Cotización guardada el {fmt_when(saved_at)}</b> comparada con los precios y "
                                f"el stock de Mouser de ahora ({datetime.now().strftime('%d-%m-%Y %H:%M')})."))
        difference = after_total - before_total
        pct = f" ({'+' if difference > 0 else ''}{fmt_num(difference / before_total * 100, 1)} %)" if before_total else ""
        color = UP_COLOR if difference > 0 else (DOWN_COLOR if difference < 0 else "#59636E")
        sign = "+" if difference > 0 else ("−" if difference < 0 else "")
        summary = QLabel(f"Total estimado: {fmt_money(before_total, currency)} → <b>{fmt_money(after_total, currency)}"
                         f"</b> <span style='color:{color}'>{sign}{fmt_money(abs(difference), currency)}{pct}</span>")
        summary.setStyleSheet("font-size: 12pt")
        layout.addWidget(summary)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        order = ["Subió", "Bajó", "Cambió la parte elegida", "Sin precio ahora", "Ahora tiene precio", "Sin cambio",
                 "Sin precio"]
        detail = " · ".join(f"{counts[s]} {s.lower()}" for s in order if counts.get(s))
        note = QLabel(detail + ". Las partes pueden subir o bajar de tramo si cambió el stock o el precio.")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = _table(self.COLUMNS)
        interesting = sorted(rows, key=lambda r: (r.status == "Sin cambio",
                                                  -abs((r.after_total or 0) - (r.before_total or 0))))
        self.table.setRowCount(len(interesting))
        for r, row in enumerate(interesting):
            change = row.change_pct
            status_color = {"Subió": UP_COLOR, "Bajó": DOWN_COLOR, "Sin precio ahora": UP_COLOR,
                            "Cambió la parte elegida": WARN_COLOR}.get(row.status)
            pns = row.after_pn if row.after_pn == row.before_pn else f"{row.before_pn or '—'} → {row.after_pn or '—'}"
            stock_tip = (f"Stock antes: {fmt_int(row.before_stock)}" if row.before_stock is not None
                         else "Stock antes: —")
            values = [
                _cell(row.label, tip=f"Designadores: {row.designators}" if row.designators else ""),
                _cell(pns),
                _cell(fmt_int(row.after_qty or row.before_qty), right=True),
                _cell(fmt_price(row.before_unit) if row.before_unit is not None else "—", right=True),
                _cell(fmt_price(row.after_unit) if row.after_unit is not None else "—", right=True),
                _cell(fmt_money(row.before_total) if row.before_total is not None else "—", right=True),
                _cell(fmt_money(row.after_total) if row.after_total is not None else "—", right=True),
                _cell(f"{'+' if change > 0 else ''}{fmt_num(change, 1)} %" if change is not None else "", right=True,
                      color=UP_COLOR if change and change > 0 else (DOWN_COLOR if change and change < 0 else None)),
                _cell(fmt_int(row.after_stock) if row.after_stock is not None else "—", right=True, tip=stock_tip,
                      color=UP_COLOR if row.after_stock is not None and row.after_stock < (row.after_qty or 0) else None),
                _cell(row.status, color=status_color, bold=row.status != "Sin cambio"),
            ]
            for c, cell in enumerate(values):
                self.table.setItem(r, c, cell)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Cerrar")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
