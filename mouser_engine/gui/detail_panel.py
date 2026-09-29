"""Panel inferior con el detalle de la parte seleccionada, sus tramos y opciones en Mouser."""

from __future__ import annotations

import html
from typing import Callable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..formatting import fmt_int, fmt_money, fmt_num, fmt_price
from ..models import BomItem, ItemQuote, Part, lead_time_label, packaging_label
from ..pricing import price_for_qty, purchase_qty
from ..quote import rank_options, rank_spec_options
from ..utils import normalize_pn
from .theme import STATUS_COLOR


def _e(text: object) -> str:
    return html.escape(str(text)) if text not in (None, "") else "—"


class DetailPanel(QTabWidget):
    use_option = Signal(object)  # Part
    auto_select = Signal()
    search_requested = Signal()
    requery_requested = Signal()

    OPTION_COLUMNS = ["", "Origen", "N° Mouser", "MPN", "Fabricante", "Empaque", "Stock", "Mín / Múlt",
                      "Precio unit.", "Total", "Ciclo de vida"]

    HISTORY_COLUMNS = ["Consultado", "BOM", "Cantidad", "Precio unit.", "Total", "Stock en Mouser", "Variación"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.item: BomItem | None = None
        self.quote: ItemQuote | None = None
        self._options: list[Part] = []
        # (parte, ítem) -> precios guardados en el historial; lo asigna la ventana principal
        self.history_provider: Callable[[Part | None, BomItem | None], list] | None = None

        self.info = QTextBrowser()
        self.info.setOpenExternalLinks(True)
        self.addTab(self.info, "Parte seleccionada")

        self.breaks = QTableWidget(0, 3)
        self.breaks.setHorizontalHeaderLabels(["Desde (unidades)", "Precio unitario", "Costo total comprando en ese tramo"])
        self.breaks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.breaks.verticalHeader().setVisible(False)
        self.breaks.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.addTab(self.breaks, "Tramos de precio")

        options_page = QWidget()
        options_layout = QVBoxLayout(options_page)
        options_layout.setContentsMargins(6, 6, 6, 6)
        self.options_hint = QLabel("")
        self.options_hint.setObjectName("Hint")
        self.options_hint.setWordWrap(True)
        options_layout.addWidget(self.options_hint)
        self.options = QTableWidget(0, len(self.OPTION_COLUMNS))
        self.options.setHorizontalHeaderLabels(self.OPTION_COLUMNS)
        self.options.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.options.setSelectionMode(QAbstractItemView.SingleSelection)
        self.options.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.options.verticalHeader().setVisible(False)
        self.options.doubleClicked.connect(self._use_selected)
        options_layout.addWidget(self.options, 1)
        buttons = QHBoxLayout()
        self.use_button = QPushButton("Usar la opción seleccionada")
        self.use_button.clicked.connect(self._use_selected)
        self.auto_button = QPushButton("Volver a selección automática")
        self.auto_button.clicked.connect(self.auto_select.emit)
        self.open_button = QPushButton("Ver en Mouser")
        self.open_button.clicked.connect(self._open_selected)
        self.search_button = QPushButton("Buscar en Mouser…")
        self.search_button.clicked.connect(self.search_requested.emit)
        self.requery_button = QPushButton("Volver a consultar")
        self.requery_button.clicked.connect(self.requery_requested.emit)
        for button in (self.use_button, self.auto_button, self.open_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.requery_button)
        buttons.addWidget(self.search_button)
        options_layout.addLayout(buttons)
        self.addTab(options_page, "Opciones en Mouser")
        self.options.itemSelectionChanged.connect(self._update_buttons)

        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(6, 6, 6, 6)
        self.history_hint = QLabel("")
        self.history_hint.setObjectName("Hint")
        self.history_hint.setWordWrap(True)
        history_layout.addWidget(self.history_hint)
        self.history_table = QTableWidget(0, len(self.HISTORY_COLUMNS))
        self.history_table.setHorizontalHeaderLabels(self.HISTORY_COLUMNS)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        history_layout.addWidget(self.history_table, 1)
        self.history_page = history_page
        self.addTab(history_page, "Historial de precios")
        self.currentChanged.connect(lambda *_: self.refresh_history())
        self.show_item(None, None)

    # --- contenido --------------------------------------------------------------

    def show_item(self, item: BomItem | None, quote: ItemQuote | None) -> None:
        self.item, self.quote = item, quote
        self._render_info()
        self._render_breaks()
        self._render_options()
        self._update_buttons()
        self.refresh_history()

    def refresh_history(self) -> None:
        """Precios guardados de la parte elegida (solo se consulta si la pestaña está a la vista)."""
        if self.currentWidget() is not self.history_page:
            return
        item, q = self.item, self.quote
        part = q.part if q else None
        records = self.history_provider(part, item) if (self.history_provider and item is not None) else []
        self.history_table.setRowCount(len(records))
        for r, record in enumerate(records):
            older = records[r + 1] if r + 1 < len(records) else None
            change = ""
            color = None
            if older is not None and older.unit_price and record.unit_price is not None:
                pct = (record.unit_price - older.unit_price) / older.unit_price * 100
                if abs(pct) >= 0.05:
                    change = f"{'▲ +' if pct > 0 else '▼ '}{fmt_num(pct, 1)} %"
                    color = "#CF222E" if pct > 0 else "#1A7F37"
            values = [
                record.when.strftime("%d-%m-%Y %H:%M"),
                record.bom_name or "—",
                fmt_int(record.buy_qty),
                fmt_price(record.unit_price, record.currency) if record.unit_price is not None else "—",
                fmt_money(record.unit_price * record.buy_qty, record.currency)
                if record.unit_price is not None else "—",
                fmt_int(record.stock) if record.stock is not None else "—",
                change,
            ]
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if c >= 2:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 6 and color:
                    cell.setForeground(QColor(color))
                if c == 1 and record.client:
                    cell.setToolTip(f"Cliente: {record.client}")
                self.history_table.setItem(r, c, cell)
        if item is None:
            hint = "Seleccione una parte para ver cómo cambió su precio en las cotizaciones guardadas."
        elif not records:
            hint = ("Esta parte todavía no aparece en el historial. Los precios se registran al guardar la "
                    "cotización (Ctrl+S), al exportarla a Excel o al crear el carro en Mouser.")
        else:
            name = part.mouser_pn if part else (item.mouser_pn or item.mpn)
            hint = (f"{len(records)} {'registro' if len(records) == 1 else 'registros'} de {name} en cotizaciones "
                    "guardadas, del más reciente al más antiguo. «Variación»: cambio del precio unitario respecto "
                    "del registro anterior (puede deberse también a otra cantidad o tramo).")
        self.history_hint.setText(hint)

    def _render_info(self) -> None:
        item, q = self.item, self.quote
        if item is None:
            self.info.setHtml("<p style='color:#59636E'>Seleccione una parte de la tabla para ver el detalle.</p>")
            return
        part = q.part if q else None
        color = STATUS_COLOR.get(q.level if q else "pending", "#59636E")
        parts_html = [f"<p><span style='color:{color}; font-weight:600'>● {_e(q.status if q else 'Pendiente')}</span>"]
        if part:
            parts_html.append(f" &nbsp; <b>{_e(part.mouser_pn)}</b> · {_e(part.manufacturer)}</p>")
            parts_html.append(f"<p>{_e(part.description)}</p>")
        else:
            parts_html.append("</p>")
        rows = []
        if part:
            on_order = ", ".join(f"{fmt_int(o.quantity)} ({o.date})" if o.date else fmt_int(o.quantity)
                                 for o in part.on_order) or "—"
            currency = part.currency
            rows += [
                ("MPN en Mouser", part.mpn),
                ("Empaque", packaging_label(part.packaging)),
                ("Ciclo de vida", part.lifecycle_label),
                ("RoHS", part.rohs),
                ("Stock en Mouser", fmt_int(part.stock) if part.stock is not None else "—"),
                ("Stock en fábrica", fmt_int(part.factory_stock) if part.factory_stock else "—"),
                ("En pedido a fábrica", on_order),
                ("Plazo de fábrica", lead_time_label(part.lead_time)),
                ("Mínimo / múltiplo", f"{fmt_int(part.min_qty)} / {fmt_int(part.mult)}"),
                ("Cantidad requerida", fmt_int(q.required)),
                ("Cantidad a comprar", fmt_int(q.buy_qty) if q.buy_qty else "—"),
                ("Precio unitario", fmt_price(q.unit_price, currency) if q.unit_price is not None else "—"),
                ("Total línea", fmt_money(q.ext_price, currency) if q.ext_price is not None else "—"),
            ]
        if item.spec is not None and not item.mpn and not item.mouser_pn:
            spec_text = item.spec.label()
            if item.spec.missing:
                spec_text += " (falta " + ", ".join(item.spec.missing) + ")"
            rows.append(("Especificación del BOM", spec_text))
            report = item.spec_report or {}
            if report.get("searches") or report.get("constructed"):
                searched = ", ".join(f"«{kw}»" for kw in report.get("searches", []))
                if report.get("constructed"):
                    searched += (" y " if searched else "") + f"{report['constructed']} números de parte de series comunes"
                rows.append(("Búsqueda en Mouser", searched))
        rows += [
            ("Líneas del BOM", item.rows_label),
            ("Designadores", item.designators),
            ("MPN en el BOM", item.mpn),
            ("Fabricante en el BOM", item.manufacturer),
            ("Descripción en el BOM", item.display_description),
            ("Cantidad por placa", fmt_int(item.qty_per_board)),
        ]
        if item.looked_up_at:
            rows.append(("Consultado", item.looked_up_at.strftime("%d-%m-%Y %H:%M:%S")))
        table = "".join(
            f"<tr><td style='color:#59636E; padding:1px 12px 1px 0'>{_e(k)}</td><td>{_e(v)}</td></tr>"
            for k, v in rows)
        parts_html.append(f"<table cellspacing='0'>{table}</table>")
        links = []
        if part and part.product_url:
            links.append(f"<a href='{html.escape(part.product_url)}'>Ver en Mouser</a>")
        if part and part.datasheet_url:
            links.append(f"<a href='{html.escape(part.datasheet_url)}'>Datasheet</a>")
        if links:
            parts_html.append("<p>" + " &nbsp;·&nbsp; ".join(links) + "</p>")
        if q and q.notes:
            parts_html.append("<p style='margin-bottom:2px'><b>Observaciones</b></p><ul style='margin-top:0'>" +
                              "".join(f"<li>{_e(n)}</li>" for n in q.notes) + "</ul>")
        self.info.setHtml("".join(parts_html))

    def _render_breaks(self) -> None:
        q = self.quote
        part = q.part if q else None
        breaks = sorted(part.price_breaks, key=lambda b: b.quantity) if part else []
        self.breaks.setRowCount(len(breaks))
        bold = QFont()
        bold.setBold(True)
        buy = q.buy_qty if q else 0
        current = q.ext_price if q else None
        for r, pb in enumerate(breaks):
            active = q is not None and q.active_break is not None and q.active_break.quantity == pb.quantity
            muted = False
            if active and current is not None:
                cost_text = f"{fmt_money(current, pb.currency)} por {fmt_int(buy)} u. (tramo aplicado)"
            elif buy and pb.quantity < buy:
                cost_text = "Tramo superado"
                muted = True
            else:
                option = price_for_qty(breaks, purchase_qty(max(pb.quantity, 1), part.min_qty, part.mult))
                cost_text = ""
                if option:
                    cost_text = f"{fmt_money(option.ext_price, pb.currency)} por {fmt_int(option.qty)} u."
                    if current is not None:
                        diff = option.ext_price - current
                        if diff < 0:
                            cost_text += f" (ahorra {fmt_money(-diff, pb.currency)})"
                        elif diff > 0:
                            cost_text += f" (+{fmt_money(diff, pb.currency)})"
            cells = [
                QTableWidgetItem(fmt_int(pb.quantity)),
                QTableWidgetItem(fmt_price(pb.price, pb.currency)),
                QTableWidgetItem(cost_text),
            ]
            for cell in cells:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if active:
                    cell.setFont(bold)
                    cell.setBackground(QColor("#E3F4E8"))
                elif muted:
                    cell.setForeground(QColor("#8C959F"))
            self.breaks.setItem(r, 0, cells[0])
            self.breaks.setItem(r, 1, cells[1])
            self.breaks.setItem(r, 2, cells[2])

    def _render_options(self) -> None:
        item, q = self.item, self.quote
        self._options = []
        if item is None:
            self.options.setRowCount(0)
            self.options_hint.setText("")
            return
        required = max(q.required if q else 1, 1)
        origin: dict[str, str] = {}
        for part in item.suggestions:
            origin[part.key] = "Sugerencia"
        for part in item.near:
            origin[part.key] = "Aproximada"
        for part in item.candidates:
            origin[part.key] = "Cumple" if item.by_spec else "Exacta"
        if item.manual_part is not None:
            origin.setdefault(item.manual_part.key, "Búsqueda")
        ranker = rank_spec_options if item.by_spec else rank_options
        exact = ranker([p for p in item.options()
                        if origin.get(p.key) in ("Exacta", "Cumple", "Aproximada", "Búsqueda")], required)
        others = rank_options([p for p in item.options() if origin.get(p.key) == "Sugerencia"], required)
        self._options = exact + others
        current_key = q.part.key if q and q.part else None
        self.options.setRowCount(len(self._options))
        bold = QFont()
        bold.setBold(True)
        for r, part in enumerate(self._options):
            qty = purchase_qty(required, part.min_qty, part.mult)
            option = price_for_qty(part.price_breaks, qty)
            selected = part.key == current_key
            values = [
                "●" if selected else "",
                origin.get(part.key, ""),
                part.mouser_pn, part.mpn, part.manufacturer, packaging_label(part.packaging),
                fmt_int(part.stock) if part.stock is not None else "—",
                "1" if part.min_qty == 1 and part.mult == 1 else f"{fmt_int(part.min_qty)} / {fmt_int(part.mult)}",
                fmt_price(option.unit_price) if option else "",
                fmt_money(option.ext_price, part.currency) if option else "",
                part.lifecycle_label,
            ]
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if c in (6, 7, 8, 9):
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if selected:
                    cell.setFont(bold)
                    cell.setBackground(QColor("#EAF2FC"))
                if c == 6 and part.stock is not None and part.stock < qty:
                    cell.setForeground(QColor("#CF222E"))
                self.options.setItem(r, c, cell)
        self.options.resizeColumnsToContents()
        manual = item.manual_part is not None
        if not self._options:
            if item.by_spec and item.spec.complete and item.lookup_state == "done":
                hint = (f"Ninguna opción en Mouser cumple «{item.spec.label()}». "
                        "Use «Buscar en Mouser…» para elegir una manualmente.")
            elif item.lookup_state == "noquery":
                hint = "Esta línea no tiene número de parte. Use «Buscar en Mouser…» para asignarle una."
            elif item.lookup_state == "pending":
                hint = "Todavía no se consulta en Mouser."
            else:
                hint = "No hay opciones en Mouser para este número de parte. Pruebe «Buscar en Mouser…»."
        elif item.by_spec:
            hint = (f"{len(self._options)} opciones cumplen «{item.spec.label()}». Orden: stock suficiente, "
                    f"fabricante reconocido y menor costo para {fmt_int(required)} unidades. "
                    + ("La opción actual fue elegida manualmente." if manual
                       else "La marcada con ● es la elegida automáticamente."))
        else:
            hint = (f"{len(self._options)} opciones en Mouser. El costo se calcula para {fmt_int(required)} unidades. "
                    + ("La opción actual fue elegida manualmente." if manual
                       else "La opción marcada con ● fue elegida automáticamente (stock suficiente y menor costo)."))
        self.options_hint.setText(hint)

    # --- acciones ---------------------------------------------------------------

    def _selected_option(self) -> Part | None:
        rows = self.options.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        return self._options[row] if 0 <= row < len(self._options) else None

    def _update_buttons(self) -> None:
        part = self._selected_option()
        has_item = self.item is not None
        self.use_button.setEnabled(part is not None and part.orderable)
        self.open_button.setEnabled(part is not None and bool(part.product_url))
        self.auto_button.setEnabled(has_item and self.item.manual_part is not None)
        self.search_button.setEnabled(has_item)
        self.requery_button.setEnabled(has_item and bool(self.item.base_query or self.item.manual_part))

    def _use_selected(self, *args) -> None:
        part = self._selected_option()
        if part is not None and part.orderable:
            self.use_option.emit(part)

    def _open_selected(self) -> None:
        part = self._selected_option()
        if part and part.product_url:
            QDesktopServices.openUrl(QUrl(part.product_url))

    def select_option_by_pn(self, mouser_pn: str) -> None:
        for row, part in enumerate(self._options):
            if normalize_pn(part.mouser_pn) == normalize_pn(mouser_pn):
                self.options.selectRow(row)
                return
