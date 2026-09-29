"""Modelo de tabla con las partes del BOM y su cotización."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont

from ..formatting import fmt_int, fmt_money, fmt_price
from ..models import (
    LEVEL_ERROR,
    LEVEL_EXCLUDED,
    LEVEL_OK,
    LEVEL_PENDING,
    LEVEL_WARN,
    BomItem,
    ItemQuote,
    lead_time_label,
)
from ..utils import parse_int
from .theme import ACCENT, ROW_BACKGROUND, STATUS_COLOR

SORT_ROLE = Qt.UserRole + 1

# (clave, título, ancho sugerido)
COLUMNS = [
    ("include", "Comprar", 80),
    ("rows", "Línea", 58),
    ("designators", "Designadores", 110),
    ("mpn", "MPN", 170),
    ("description", "Descripción", 200),
    ("qty_per_board", "Cant./placa", 92),
    ("required", "Requerida", 88),
    ("buy_qty", "A comprar", 88),
    ("unit_price", "Precio unit.", 100),
    ("ext_price", "Total", 112),
    ("status", "Estado", 180),
    ("stock", "Stock", 92),
    ("mouser_pn", "N° Mouser", 170),
    ("manufacturer", "Fabricante", 130),
    ("lead", "Plazo fábrica", 104),
    ("minmult", "Mín / Múlt", 94),
]
COL = {key: index for index, (key, _, _) in enumerate(COLUMNS)}
NUMERIC = {"qty_per_board", "required", "stock", "buy_qty", "unit_price", "ext_price", "rows"}
EDITABLE = {"qty_per_board", "mpn", "mouser_pn", "manufacturer"}


class ItemsModel(QAbstractTableModel):
    item_edited = Signal(object, str)  # (BomItem, campo)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[BomItem] = []
        self.quotes: list[ItemQuote] = []

    # --- carga de datos ---------------------------------------------------

    def set_items(self, items: list[BomItem], quotes: list[ItemQuote]) -> None:
        self.beginResetModel()
        self.items = items
        self.quotes = quotes
        self.endResetModel()

    def set_quotes(self, quotes: list[ItemQuote]) -> None:
        self.quotes = quotes
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLUMNS) - 1))

    def item_at(self, row: int) -> BomItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def quote_at(self, row: int) -> ItemQuote | None:
        return self.quotes[row] if 0 <= row < len(self.quotes) else None

    def row_of(self, item: BomItem) -> int:
        for index, candidate in enumerate(self.items):
            if candidate is item:
                return index
        return -1

    # --- QAbstractTableModel ----------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return COLUMNS[section][1]
            if role == Qt.TextAlignmentRole:
                key = COLUMNS[section][0]
                return int((Qt.AlignRight if key in NUMERIC else Qt.AlignLeft) | Qt.AlignVCenter)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        key = COLUMNS[index.column()][0]
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if key == "include":
            flags |= Qt.ItemIsUserCheckable
        if key in EDITABLE:
            flags |= Qt.ItemIsEditable
        return flags

    def _values(self, item: BomItem, q: ItemQuote | None, key: str):
        """Devuelve (texto a mostrar, valor para ordenar)."""
        part = q.part if q else None
        currency = q.currency if q else ""
        if key == "include":
            return "", 1 if item.include else 0
        if key == "rows":
            return item.rows_label, item.rows[0] if item.rows else 0
        if key == "designators":
            return item.designators, item.designators.lower()
        if key == "mpn":
            if item.by_spec and not item.mpn and part is not None:
                return part.mpn, part.mpn.lower()
            return item.mpn, item.mpn.lower()
        if key == "manufacturer":
            text = item.manufacturer or (part.manufacturer if part else "")
            return text, text.lower()
        if key == "description":
            text = item.display_description or (part.description if part else "")
            return text, text.lower()
        if key == "qty_per_board":
            return fmt_int(item.qty_per_board), item.qty_per_board
        if key == "required":
            return fmt_int(q.required) if q else "", q.required if q else 0
        if key == "mouser_pn":
            text = part.mouser_pn if part else item.mouser_pn
            return text, text.lower()
        if key == "stock":
            if not part or part.stock is None:
                return ("—" if part else ""), -1
            return fmt_int(part.stock), part.stock
        if key == "lead":
            if not part:
                return "", 10**6
            return lead_time_label(part.lead_time), part.lead_time_days if part.lead_time_days is not None else 10**6
        if key == "minmult":
            if not part:
                return "", 0
            if part.min_qty == 1 and part.mult == 1:
                return "1", 1
            return f"{fmt_int(part.min_qty)} / {fmt_int(part.mult)}", part.min_qty
        if key == "buy_qty":
            return (fmt_int(q.buy_qty) if q and q.buy_qty else ""), (q.buy_qty if q else 0)
        if key == "unit_price":
            value = q.unit_price if q else None
            return (fmt_price(value) if value is not None else ""), float(value) if value is not None else -1.0
        if key == "ext_price":
            value = q.ext_price if q else None
            return (fmt_money(value, currency) if value is not None else ""), float(value) if value is not None else -1.0
        if key == "status":
            level_rank = {LEVEL_ERROR: 0, LEVEL_WARN: 1, LEVEL_PENDING: 2, LEVEL_OK: 3, LEVEL_EXCLUDED: 4}
            return (q.status if q else ""), level_rank.get(q.level if q else LEVEL_PENDING, 5)
        return "", ""

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        q = self.quote_at(index.row())
        key = COLUMNS[index.column()][0]
        level = q.level if q else LEVEL_PENDING

        if role == Qt.DisplayRole:
            return self._values(item, q, key)[0]
        if role == SORT_ROLE:
            return self._values(item, q, key)[1]
        if role == Qt.EditRole:
            if key == "qty_per_board":
                return item.qty_per_board
            if key == "mpn":
                return item.mpn
            if key == "mouser_pn":
                return item.mouser_pn or (q.part.mouser_pn if q and q.part else "")
            if key == "manufacturer":
                return item.manufacturer
            return self._values(item, q, key)[0]
        if role == Qt.CheckStateRole and key == "include":
            return Qt.Checked if item.include else Qt.Unchecked
        if role == Qt.TextAlignmentRole:
            if key in NUMERIC or key == "minmult":
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if key == "include":
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.BackgroundRole:
            color = ROW_BACKGROUND.get(level)
            return QBrush(QColor(color)) if color else None
        if role == Qt.ForegroundRole:
            if level == LEVEL_EXCLUDED:
                return QBrush(QColor("#8C959F"))
            if key == "status":
                return QBrush(QColor(STATUS_COLOR.get(level, "#1F2328")))
            if key == "stock" and q and q.part and q.part.stock is not None and q.buy_qty and q.part.stock < q.buy_qty:
                return QBrush(QColor(STATUS_COLOR[LEVEL_ERROR]))
            if key == "mpn" and item.by_spec and not item.mpn:
                return QBrush(QColor(ACCENT))
            return None
        if role == Qt.FontRole:
            if key in ("status", "ext_price"):
                font = QFont()
                font.setBold(True)
                return font
            if key == "mpn" and item.by_spec and not item.mpn:
                font = QFont()
                font.setItalic(True)
                return font
            return None
        if role == Qt.ToolTipRole:
            lines = []
            if q and q.status:
                lines.append(f"<b>{q.status}</b>")
            if q:
                lines.extend(q.notes)
            if key == "description" and (item.display_description or (q and q.part)):
                lines.insert(0, item.display_description or q.part.description)
            if key == "mpn" and item.by_spec and not item.mpn:
                lines.insert(0, "Elegida automáticamente por especificación: " + item.spec.label()
                             + "<br>Escriba un MPN para reemplazarla.")
            return "<br>".join(lines) if lines else None
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        item = self.items[index.row()]
        key = COLUMNS[index.column()][0]
        if key == "include" and role == Qt.CheckStateRole:
            checked = value == Qt.Checked or value == Qt.Checked.value or value is True
            item.include = bool(checked)
            self._emit_row(index.row())
            self.item_edited.emit(item, "include")
            return True
        if role != Qt.EditRole:
            return False
        text = str(value if value is not None else "").strip()
        if key == "qty_per_board":
            qty = parse_int(text)
            if qty is None or qty < 0:
                return False
            item.qty_per_board = qty
            if qty > 0 and not item.include and item.lookup_state != "noquery":
                item.include = True
        elif key == "mpn":
            if text == item.mpn:
                return False
            item.mpn = text
        elif key == "mouser_pn":
            if text == (item.mouser_pn or ""):
                return False
            item.mouser_pn = text
        elif key == "manufacturer":
            if text == item.manufacturer:
                return False
            item.manufacturer = text
        else:
            return False
        self._emit_row(index.row())
        self.item_edited.emit(item, key)
        return True

    def _emit_row(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))


STATUS_FILTERS = [
    ("Todas las partes", None),
    ("Con problemas (errores y advertencias)", {LEVEL_ERROR, LEVEL_WARN}),
    ("Solo errores", {LEVEL_ERROR}),
    ("Solo advertencias", {LEVEL_WARN}),
    ("OK", {LEVEL_OK}),
    ("Pendientes", {LEVEL_PENDING}),
    ("Excluidas", {LEVEL_EXCLUDED}),
]


class ItemsFilterModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._levels: set[str] | None = None
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(False)

    def _change_filter(self, apply) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt >= 6.9
            self.beginFilterChange()
            apply()
            self.endFilterChange()
        else:  # pragma: no cover - Qt anteriores
            apply()
            self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._change_filter(lambda: setattr(self, "_text", text.strip().lower()))

    def set_levels(self, levels: set[str] | None) -> None:
        self._change_filter(lambda: setattr(self, "_levels", levels))

    def refresh(self) -> None:
        """Vuelve a evaluar el filtro (p. ej. después de recalcular estados)."""
        self._change_filter(lambda: None)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: ItemsModel = self.sourceModel()  # type: ignore[assignment]
        item = model.item_at(source_row)
        if item is None:
            return False
        q = model.quote_at(source_row)
        if self._levels is not None and (q.level if q else LEVEL_PENDING) not in self._levels:
            return False
        if self._text:
            part = q.part if q else None
            haystack = " ".join([
                item.designators, item.mpn, item.manufacturer, item.description, item.value,
                item.mouser_pn, part.mouser_pn if part else "", part.mpn if part else "",
                part.description if part else "", q.status if q else "",
            ]).lower()
            return all(word in haystack for word in self._text.split())
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = left.data(SORT_ROLE)
        b = right.data(SORT_ROLE)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)
