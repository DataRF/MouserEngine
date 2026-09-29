"""Diálogos para crear el carro en Mouser: confirmación previa y resultado.

La aplicación solo crea el carro (Cart API). Nunca envía un pedido: la compra se revisa y
se confirma en mouser.com.
"""

from __future__ import annotations

import html
from decimal import Decimal

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..cart import CartLine, CartResult
from ..formatting import fmt_int, fmt_money, fmt_price

MOUSER_CART_URL = "https://www.mouser.com/Cart/"
ERROR_COLOR = "#CF222E"
WARN_COLOR = "#9A6700"
OK_COLOR = "#1A7F37"


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(24)
    table.setWordWrap(False)
    return table


def _cell(text: str, right: bool = False, color: str | None = None, tip: str = "") -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    if right:
        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if color:
        cell.setForeground(QColor(color))
    if tip:
        cell.setToolTip(tip)
    return cell


def _label(text: str, hint: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    if hint:
        label.setObjectName("Hint")
    return label


class CartConfirmDialog(QDialog):
    """Muestra lo que se enviará a Mouser y pide confirmación."""

    def __init__(self, lines: list[CartLine], currency: str, notes: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crear carro en Mouser")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        known = [line.ext_price for line in lines if line.ext_price is not None]
        total = sum(known, Decimal("0"))
        layout.addWidget(_label(
            f"Se creará un <b>carro nuevo</b> en su cuenta de Mouser con <b>{fmt_int(len(lines))} "
            f"{'ítem' if len(lines) == 1 else 'ítems'}</b> (total estimado <b>{fmt_money(total, currency)}</b>)."))
        layout.addWidget(_label(
            "<b>No se realiza ningún pedido.</b> El carro queda en mouser.com para revisarlo y, si "
            "corresponde, comprarlo desde la web. Mouser confirma el precio y la disponibilidad al crearlo.",
            hint=True))
        for note in notes:
            layout.addWidget(_label(f"<span style='color:{WARN_COLOR}'>⚠ {html.escape(note)}</span>"))

        self.table = _table(["N° Mouser", "MPN", "Fabricante", "Cantidad", "Referencia", "Total estimado"])
        self.table.setRowCount(len(lines))
        for r, line in enumerate(lines):
            values = [
                _cell(line.mouser_pn),
                _cell(line.mpn),
                _cell(line.manufacturer),
                _cell(fmt_int(line.quantity), right=True),
                _cell(line.customer_pn, tip=line.designators),
                _cell(fmt_money(line.ext_price, line.currency) if line.ext_price is not None else "—", right=True),
            ]
            for c, cell in enumerate(values):
                self.table.setItem(r, c, cell)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        layout.addWidget(_label(
            "La «Referencia» (designadores, hasta 21 caracteres) se guarda en cada ítem del carro como "
            "número de parte del cliente.", hint=True))

        buttons = QDialogButtonBox()
        self.create_button = buttons.addButton("Crear carro", QDialogButtonBox.AcceptRole)
        self.create_button.setObjectName("Primary")
        self.create_button.setDefault(True)
        buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CartResultDialog(QDialog):
    """Clave del carro creado, precios que confirmó Mouser y observaciones por ítem."""

    def __init__(self, result: CartResult, local_total: Decimal | None, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle("Carro en Mouser")
        self.resize(980, 600)
        layout = QVBoxLayout(self)

        issues = len(result.items_with_errors) + len(result.pending) + len(result.missing) + len(result.errors)
        if result.cart_key and not issues:
            title = f"<span style='color:{OK_COLOR}'>✔</span> <b>Carro creado en Mouser</b>"
        elif result.cart_key:
            title = f"<span style='color:{WARN_COLOR}'>⚠</span> <b>Carro creado con observaciones</b>"
        else:
            title = f"<span style='color:{ERROR_COLOR}'>✖</span> <b>Mouser no creó el carro</b>"
        layout.addWidget(_label(f"<span style='font-size:13pt'>{title}</span>"))

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("Clave del carro (CartKey):"))
        self.key_edit = QLineEdit(result.cart_key or "—")
        self.key_edit.setReadOnly(True)
        key_row.addWidget(self.key_edit, 1)
        copy = QPushButton("Copiar")
        copy.setEnabled(bool(result.cart_key))
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(result.cart_key))
        key_row.addWidget(copy)
        layout.addLayout(key_row)

        total_text = f"Total según Mouser: <b>{fmt_money(result.total, result.currency)}</b>"
        if local_total is not None and result.items:
            difference = result.total - local_total
            if abs(difference) >= Decimal("0.01"):
                sign = "+" if difference > 0 else "−"
                total_text += (f" · la cotización estimaba {fmt_money(local_total, result.currency)} "
                               f"({sign}{fmt_money(abs(difference), result.currency)})")
            else:
                total_text += " · igual a la cotización"
        layout.addWidget(_label(total_text))

        for message in result.errors:
            layout.addWidget(_label(f"<span style='color:{ERROR_COLOR}'>✖ {html.escape(message)}</span>"))
        if result.pending:
            layout.addWidget(_label(
                f"<span style='color:{ERROR_COLOR}'>✖ {len(result.pending)} ítems no se alcanzaron a enviar: "
                + html.escape(", ".join(line.mouser_pn for line in result.pending[:12]))
                + ("…" if len(result.pending) > 12 else "") + "</span>"))
        if result.missing:
            layout.addWidget(_label(
                f"<span style='color:{WARN_COLOR}'>⚠ Mouser no devolvió estos ítems: "
                + html.escape(", ".join(line.mouser_pn for line in result.missing)) + "</span>"))

        self.table = _table(["N° Mouser", "MPN", "Cantidad", "Precio unit.", "Total", "Disponible", "Observaciones"])
        self.table.setRowCount(len(result.items))
        for r, item in enumerate(result.items):
            notes = item.errors + item.info
            color = ERROR_COLOR if item.errors else (WARN_COLOR if item.info else None)
            short = item.available is not None and item.available < item.quantity
            values = [
                _cell(item.mouser_pn, color=ERROR_COLOR if item.errors else None),
                _cell(item.mpn),
                _cell(fmt_int(item.quantity), right=True),
                _cell(fmt_price(item.unit_price) if item.unit_price else "—", right=True),
                _cell(fmt_money(item.ext_price) if item.ext_price else "—", right=True),
                _cell(fmt_int(item.available) if item.available is not None else "—", right=True,
                      color=WARN_COLOR if short else None),
                _cell(" · ".join(notes) or "OK", color=color or OK_COLOR, tip="\n".join(notes)),
            ]
            for c, cell in enumerate(values):
                self.table.setItem(r, c, cell)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        layout.addWidget(_label(
            "Para revisar el carro y comprar, ingrese a mouser.com con la cuenta de My Mouser a la que "
            "pertenece la clave de Cart API. Esta aplicación no envía pedidos. Si el carro no aparece, "
            "use «Exportar carro Mouser» (CSV) y cárguelo con la herramienta de carga de BOM de mouser.com.",
            hint=True))

        buttons = QDialogButtonBox()
        open_button = buttons.addButton("Abrir mouser.com", QDialogButtonBox.ActionRole)
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(MOUSER_CART_URL)))
        close = buttons.addButton("Cerrar", QDialogButtonBox.AcceptRole)
        close.setDefault(True)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
