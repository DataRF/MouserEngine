"""Informe PDF para el cliente, dibujado con Qt (QPdfWriter + QPainter).

Todo se dibuja en coordenadas de 96 ppp (como la pantalla) sobre un QPainter escalado. Así el
gráfico usa exactamente el mismo código que la aplicación y el PDF queda vectorial: texto
seleccionable y gráfico nítido a cualquier zoom.

La diagramación se hace en dos pasadas: primero se ubica cada elemento en su página (midiendo el
texto con el mismo QPainter) y después se dibuja, cuando ya se conoce el total de páginas para el
pie «Página X de Y».
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPageSize, QPainter, QPdfWriter, QPen, QTextOption
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..formatting import fmt_board_cost, fmt_int, fmt_money, fmt_num, fmt_price
from ..models import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN
from ..report import ClientReport, ReportLine
from .scenario_chart import ChartPainter
from .theme import SERIES_1

PAGE_SIZES = {"letter": QPageSize.Letter, "a4": QPageSize.A4}
PAGE_LABELS = {"letter": "Carta", "a4": "A4"}

MARGIN_X = 60
MARGIN_TOP = 52
MARGIN_BOTTOM = 58
HEADER_SPACE = 30  # desde la página 2: nombre del proyecto arriba

INK = "#1F2328"
MUTED = "#59636E"
RULE = "#D0D7DE"
HEAD_BG = "#EEF1F4"
ZEBRA = "#F7F8FA"
ACCENT = "#1F3A5F"
LEVEL_COLORS = {LEVEL_OK: "#1A7F37", LEVEL_WARN: "#9A6700", LEVEL_ERROR: "#CF222E"}
BIG_DROP = -10  # % de baja del costo por placa que se destaca


def _font(size: int, bold: bool = False, italic: bool = False) -> QFont:
    """Fuente de la interfaz, en píxeles de 96 ppp y sin ajuste a la grilla (medidas exactas en el PDF)."""
    app = QApplication.instance()
    font = QFont(app.font().family()) if app is not None else QFont()
    font.setPixelSize(size)
    font.setBold(bold)
    font.setItalic(italic)
    font.setHintingPreference(QFont.PreferNoHinting)
    if hasattr(font, "setFeature"):  # sin ligaduras («fl», «fi»): el texto del PDF se busca y copia tal cual
        for tag in ("liga", "clig"):
            font.setFeature(QFont.Tag(tag), 0)
    return font


@dataclass
class Column:
    title: str
    width: float
    align: Qt.AlignmentFlag = Qt.AlignLeft


@dataclass
class Cell:
    text: str
    color: str | None = None
    bold: bool = False
    sub: str = ""  # segunda línea, más chica y gris
    bar: float | None = None  # 0..1: barra de proporción detrás del texto
    align: Qt.AlignmentFlag | None = None


class Layout:
    """Ubica texto, tablas y bloques en páginas; cada página es una lista de operaciones de dibujo."""

    def __init__(self, painter: QPainter, page_width: float, page_height: float):
        self.painter = painter
        self.page_width = page_width
        self.page_height = page_height
        self.left = MARGIN_X
        self.width = page_width - 2 * MARGIN_X
        self.bottom = page_height - MARGIN_BOTTOM
        self.pages: list[list[Callable[[QPainter], None]]] = []
        self.y = 0.0
        self.new_page()

    # --- páginas ---------------------------------------------------------------------

    @property
    def page_top(self) -> float:
        return MARGIN_TOP + (HEADER_SPACE if len(self.pages) > 1 else 0)

    def new_page(self) -> None:
        self.pages.append([])
        self.y = self.page_top

    def ensure(self, height: float) -> None:
        """Pasa a una página nueva si no cabe `height` (salvo que ya se esté al inicio de una)."""
        if self.y + height > self.bottom and self.y > self.page_top + 1:
            self.new_page()

    def add(self, op: Callable[[QPainter], None]) -> None:
        self.pages[-1].append(op)

    def space(self, height: float) -> None:
        self.y += height

    # --- medidas ---------------------------------------------------------------------

    @staticmethod
    def option(align: Qt.AlignmentFlag = Qt.AlignLeft, anywhere: bool = True) -> QTextOption:
        """Alineación y corte de línea: entre palabras y, si una palabra no cabe, donde sea necesario."""
        option = QTextOption(align | Qt.AlignTop)
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere if anywhere else QTextOption.WordWrap)
        return option

    def measure(self, font: QFont, text: str, width: float, align: Qt.AlignmentFlag = Qt.AlignLeft,
                anywhere: bool = True) -> float:
        if not text:
            return 0.0
        self.painter.setFont(font)
        return self.painter.boundingRect(QRectF(0, 0, max(1.0, width), 1e6), text,
                                         self.option(align, anywhere)).height()

    # --- elementos -------------------------------------------------------------------

    def text(self, text: str, font: QFont, color: str = INK, align: Qt.AlignmentFlag = Qt.AlignLeft,
             x: float | None = None, width: float | None = None, after: float = 6, keep: float = 0) -> float:
        width = width if width is not None else self.width
        x = self.left if x is None else x
        height = self.measure(font, text, width, align)
        self.ensure(height + keep)
        y = self.y
        option = self.option(align)

        def draw(p: QPainter) -> None:
            p.setFont(font)
            p.setPen(QColor(color))
            p.drawText(QRectF(x, y, width, height + 2), text, option)

        self.add(draw)
        self.y += height + after
        return height

    def heading(self, text: str, keep: float = 90) -> None:
        self.space(6)
        self.text(text, _font(15, bold=True), ACCENT, after=8, keep=keep)

    def bullets(self, items: list[str], font: QFont, color: str = INK, after: float = 3) -> None:
        indent = 14
        for item in items:
            height = self.measure(font, item, self.width - indent)
            self.ensure(height)
            y = self.y

            def draw(p: QPainter, y=y, item=item, height=height) -> None:
                p.setFont(font)
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(self.left, y, indent, height), "•", self.option())
                p.setPen(QColor(color))
                p.drawText(QRectF(self.left + indent, y, self.width - indent, height + 2), item, self.option())

            self.add(draw)
            self.y += height + after

    def rule(self, after: float = 10, color: str = RULE) -> None:
        y = self.y

        def draw(p: QPainter) -> None:
            p.setPen(QPen(QColor(color), 1))
            p.drawLine(QPointF(self.left, y), QPointF(self.left + self.width, y))

        self.add(draw)
        self.y += after

    def block(self, height: float, draw: Callable[[QPainter, QRectF], None], after: float = 10) -> None:
        self.ensure(height)
        rect = QRectF(self.left, self.y, self.width, height)
        self.add(lambda p: draw(p, rect))
        self.y += height + after

    def table(self, columns: list[Column], rows: list[list[Cell]], font: QFont | None = None,
              head_font: QFont | None = None, zebra: bool = True, pad: float = 4, after: float = 10,
              highlight: set[int] | None = None) -> None:
        """Tabla con encabezado que se repite en cada página. `highlight`: filas con fondo destacado."""
        font = font or _font(11)
        head_font = head_font or _font(10, bold=True)
        sub_font = _font(10)
        bold_font = QFont(font)
        bold_font.setBold(True)
        total = sum(c.width for c in columns)
        widths = [c.width * self.width / total for c in columns]
        xs = [self.left + sum(widths[:i]) for i in range(len(columns))]
        head_height = max(self.measure(head_font, c.title, w - 2 * pad, c.align, anywhere=False)
                          for c, w in zip(columns, widths))
        head_height += 2 * pad + 2

        def measure_row(row: list[Cell]) -> tuple[float, list[float]]:
            """Alto de la fila y alto del texto principal de cada celda (el resto es la segunda línea)."""
            height, mains = 0.0, []
            for cell, column, width in zip(row, columns, widths):
                align = cell.align if cell.align is not None else column.align
                main = self.measure(bold_font if cell.bold else font, cell.text, width - 2 * pad, align)
                mains.append(main)
                sub = self.measure(sub_font, cell.sub, width - 2 * pad, align) + 1 if cell.sub else 0
                height = max(height, main + sub)
            return height + 2 * pad, mains

        def draw_header(y: float) -> None:
            def draw(p: QPainter) -> None:
                p.fillRect(QRectF(self.left, y, self.width, head_height), QColor(HEAD_BG))
                p.setPen(QPen(QColor(RULE), 0.8))
                p.drawLine(QPointF(self.left, y + head_height), QPointF(self.left + self.width, y + head_height))
                p.setFont(head_font)
                p.setPen(QColor(INK))
                for column, x, width in zip(columns, xs, widths):
                    p.drawText(QRectF(x + pad, y + pad + 1, width - 2 * pad, head_height - 2 * pad), column.title,
                               self.option(column.align, anywhere=False))
            self.add(draw)

        measured = [measure_row(row) for row in rows]
        self.ensure(head_height + (measured[0][0] if measured else 0))
        draw_header(self.y)
        self.y += head_height
        for index, (row, (height, mains)) in enumerate(zip(rows, measured)):
            if self.y + height > self.bottom:
                self.new_page()
                draw_header(self.y)
                self.y += head_height
            y = self.y
            shade = HEAD_BG if highlight and index in highlight else (ZEBRA if zebra and index % 2 else None)

            def draw(p: QPainter, row=row, y=y, height=height, mains=mains, shade=shade) -> None:
                if shade:
                    p.fillRect(QRectF(self.left, y, self.width, height), QColor(shade))
                p.setPen(QPen(QColor(RULE), 0.5))
                p.drawLine(QPointF(self.left, y + height), QPointF(self.left + self.width, y + height))
                for cell, column, x, width, main in zip(row, columns, xs, widths, mains):
                    align = cell.align if cell.align is not None else column.align
                    inner = QRectF(x + pad, y + pad, width - 2 * pad, height - 2 * pad)
                    if cell.bar is not None and cell.bar > 0:
                        bar = QColor(SERIES_1)
                        bar.setAlphaF(0.22)
                        p.fillRect(QRectF(inner.left(), inner.top() + 1, inner.width() * min(1.0, cell.bar),
                                          inner.height() - 2), bar)
                    p.setFont(bold_font if cell.bold else font)
                    p.setPen(QColor(cell.color or INK))
                    p.drawText(QRectF(inner.left(), inner.top(), inner.width(), main + 2), cell.text,
                               self.option(align))
                    if cell.sub:
                        p.setFont(sub_font)
                        p.setPen(QColor(MUTED))
                        p.drawText(QRectF(inner.left(), inner.top() + main + 1, inner.width(), inner.height() - main),
                                   cell.sub, self.option(align))

            self.add(draw)
            self.y += height
        self.y += after


# --- Informe ----------------------------------------------------------------------------

def _level_color(level: str) -> str | None:
    return LEVEL_COLORS.get(level)


def _status(line: ReportLine) -> str:
    return "OK" if line.status.startswith("OK") else line.status


def _title_block(layout: Layout, report: ClientReport) -> None:
    layout.text("INFORME DE COSTOS DE COMPONENTES", _font(10, bold=True), ACCENT, after=2)
    layout.text("Análisis de costo por volumen de fabricación", _font(22, bold=True), INK, after=2)
    if report.project:
        layout.text(report.project, _font(14), MUTED, after=10)
    else:
        layout.space(8)
    layout.rule(after=10)

    queried = report.queried_at.strftime("%d-%m-%Y %H:%M") if report.queried_at else "—"
    summary = report.summary
    pairs = [
        ("Cliente", report.client or "—"),
        ("Fecha del informe", report.generated_at.strftime("%d-%m-%Y")),
        ("Cantidad de referencia", f"{fmt_int(report.boards)} {'placa' if report.boards == 1 else 'placas'}"),
        ("Precios consultados", queried),
        ("Partes distintas", f"{fmt_int(summary.included)} ({fmt_int(summary.priced)} con precio)"),
        ("Fuente y moneda", f"Mouser Electronics · {report.currency or '—'}"),
    ]
    label_font, value_font = _font(10), _font(12, bold=True)
    column_width = layout.width / 3
    row_height = 36
    rows = (len(pairs) + 2) // 3
    top = layout.y

    def draw(p: QPainter) -> None:
        for index, (label, value) in enumerate(pairs):
            row, col = divmod(index, 3)
            x = layout.left + col * column_width
            y = top + row * row_height
            p.setFont(label_font)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(x, y, column_width - 10, 14), label, layout.option())
            p.setFont(value_font)
            p.setPen(QColor(INK))
            p.drawText(QRectF(x, y + 14, column_width - 10, 20), value, layout.option())

    layout.add(draw)
    layout.space(rows * row_height + 8)


def _kpis(layout: Layout, report: ClientReport) -> None:
    currency = report.currency
    summary = report.summary
    tiles = [("Costo por placa", fmt_board_cost(report.cost_per_board, currency),
              f"a {fmt_int(report.boards)} {'placa' if report.boards == 1 else 'placas'}"),
             ("Costo total", fmt_money(summary.total, currency),
              f"{fmt_int(report.boards)} {'placa' if report.boards == 1 else 'placas'}")]
    cheapest = report.cheapest
    if cheapest is not None and cheapest.boards != report.boards and report.cost_per_board:
        change = (cheapest.total_unit - report.cost_per_board) / report.cost_per_board * 100
        tiles.append((f"Costo por placa a {fmt_int(cheapest.boards)}", fmt_board_cost(cheapest.total_unit, currency),
                      f"{'+' if change > 0 else ''}{fmt_num(change, 1)} % vs. {fmt_int(report.boards)} placas"))
    missing = summary.unpriced
    tiles.append(("Partes con precio", f"{fmt_int(summary.priced)} de {fmt_int(summary.included)}",
                  f"{fmt_int(missing)} sin precio (no incluidas)" if missing else "todas incluidas"))
    gap = 10
    width = (layout.width - gap * (len(tiles) - 1)) / len(tiles)
    inner_width = width - 20
    height = 66
    label_font, sub_font = _font(10), _font(10)

    def fitted(text: str, font: QFont) -> str:
        return QFontMetricsF(font).elidedText(text, Qt.ElideRight, inner_width)

    def value_font(value: str) -> QFont:  # el valor se achica si no cabe (montos grandes, hoja A4)
        for size in (18, 17, 16, 15, 14, 13):
            font = _font(size, bold=True)
            if QFontMetricsF(font).horizontalAdvance(value) <= inner_width:
                return font
        return _font(13, bold=True)

    def draw(p: QPainter, rect: QRectF) -> None:
        for index, (label, value, sub) in enumerate(tiles):
            box = QRectF(rect.left() + index * (width + gap), rect.top(), width, rect.height())
            p.setPen(QPen(QColor(RULE), 0.8))
            p.setBrush(QColor("#FFFFFF"))
            p.drawRoundedRect(box, 6, 6)
            p.setBrush(Qt.NoBrush)
            inner = box.adjusted(10, 7, -10, -6)
            p.setFont(label_font)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(inner.left(), inner.top(), inner.width(), 14), fitted(label, label_font),
                       layout.option())
            p.setFont(value_font(value))
            p.setPen(QColor(INK))
            p.drawText(QRectF(inner.left(), inner.top() + 14, inner.width(), 26), value, layout.option())
            p.setFont(sub_font)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(inner.left(), inner.top() + 40, inner.width(), 14), fitted(sub, sub_font),
                       layout.option())

    layout.block(height, draw, after=14)


def _chart(layout: Layout, report: ClientReport) -> None:
    renderer = ChartPainter()
    renderer.set_mode(report.chart_mode)
    renderer.set_data(report.curve, report.currency, report.quantities, report.max_boards)
    if not renderer.has_data:
        return
    layout.heading("Costo según la cantidad a fabricar", keep=320)
    height = 300 if report.chart_mode == "overlay" else 380
    reference = renderer.point_at(report.boards)

    def draw(p: QPainter, rect: QRectF) -> None:
        p.save()
        p.translate(rect.topLeft())
        p.setRenderHint(QPainter.Antialiasing)
        renderer.paint(p, rect.width(), rect.height(), _font(11), _font(10), cursor=None, shown=reference,
                       readout=False, markers=True)
        p.restore()

    layout.block(height, draw, after=4)
    caption = ("Los puntos marcan las cantidades comparadas en la tabla y la línea vertical, la cantidad de "
               f"referencia ({fmt_int(report.boards)} placas). Eje de cantidad en escala logarítmica.")
    layout.text(caption, _font(10), MUTED, after=10)


def _scenario_table(layout: Layout, report: ClientReport) -> None:
    if not report.scenarios:
        return
    currency = report.currency
    layout.heading("Escenarios comparados")
    columns = [Column("Placas", 0.8, Qt.AlignRight), Column(f"Costo por placa ({currency})", 1.25, Qt.AlignRight),
               Column("Variación por placa", 1.05, Qt.AlignRight), Column(f"Costo total ({currency})", 1.25, Qt.AlignRight),
               Column(f"Solo componentes ({currency})", 1.3, Qt.AlignRight),
               Column("Partes sin stock suficiente", 1.15, Qt.AlignRight), Column("Partes sin precio", 0.95, Qt.AlignRight)]
    rows, highlight = [], set()
    for index, row in enumerate(report.scenarios):
        change = ""
        color = None
        if row.change is not None:
            arrow = "▼ " if row.change < 0 else ("▲ " if row.change > 0 else "")
            change = f"{arrow}{fmt_num(row.change, 1)} %"
            if row.change <= BIG_DROP:
                color = LEVEL_COLORS[LEVEL_OK]
        if row.boards == report.boards:
            highlight.add(index)
        rows.append([
            Cell(fmt_int(row.boards), bold=row.boards == report.boards),
            Cell(fmt_board_cost(row.total_unit) if row.total_unit is not None else "—", bold=True),
            Cell(change, color=color, bold=color is not None),
            Cell(fmt_money(row.total)),
            Cell(fmt_money(row.goods)),
            Cell(fmt_int(row.short) if row.short else "—", color=LEVEL_COLORS[LEVEL_WARN] if row.short else None),
            Cell(fmt_int(row.unpriced) if row.unpriced else "—",
                 color=LEVEL_COLORS[LEVEL_ERROR] if row.unpriced else None),
        ])
    layout.table(columns, rows, highlight=highlight, zebra=False)
    notes = ["En verde, bajas de 10 % o más del costo por placa respecto de la cantidad anterior."]
    if report.boards in report.quantities:
        notes.append("La fila destacada es la cantidad de referencia.")
    if any(row.unpriced for row in report.scenarios):
        notes.append("Las partes sin precio no se suman.")
    layout.text(" ".join(notes), _font(10), MUTED, after=8)


def _top_parts(layout: Layout, report: ClientReport) -> None:
    top = report.top
    if len(top) < 2:
        return
    currency = report.currency
    layout.heading("Partes que más influyen en el costo")
    share = sum(line.share or 0 for line in top[:3])
    count = min(3, len(top))
    layout.text(f"A {fmt_int(report.boards)} placas, las {count} partes de mayor costo suman el "
                f"{fmt_num(share, 0)} % del costo de componentes.", _font(12), INK, after=8)
    columns = [Column("Parte", 2.2), Column("Designadores", 1.6), Column("A comprar", 0.8, Qt.AlignRight),
               Column(f"Precio unit. ({currency})", 1.0, Qt.AlignRight), Column(f"Total ({currency})", 1.0, Qt.AlignRight),
               Column("% del costo", 1.2, Qt.AlignRight)]
    maximum = max(float(line.share or 0) for line in top) or 1.0
    rows = [[Cell(line.mpn, bold=True, sub=line.manufacturer), Cell(line.designators),
             Cell(fmt_int(line.buy_qty)), Cell(fmt_price(line.unit_price)), Cell(fmt_money(line.ext_price)),
             Cell(f"{fmt_num(line.share, 1)} %", bar=float(line.share or 0) / maximum)] for line in top]
    layout.table(columns, rows, zebra=False)


def _observations(layout: Layout, report: ClientReport) -> None:
    layout.heading("Observaciones")
    if not report.observations:
        layout.text(f"Todas las partes tienen precio y stock suficiente en Mouser para {fmt_int(report.boards)} "
                    "placas.", _font(12), INK, after=8)
        return
    for observation in report.observations:
        layout.text(observation.title, _font(12, bold=True), _level_color(observation.level) or INK, after=3,
                    keep=30)
        layout.bullets(observation.items, _font(11))
        layout.space(6)


def _price_matrix(layout: Layout, report: ClientReport) -> None:
    lines = [line for line in report.lines if any(price is not None for price in line.prices)]
    if not lines or not report.quantities:
        return
    layout.heading(f"Precio unitario según la cantidad ({report.currency})")
    layout.text("Precio por unidad de cada parte según la cantidad de placas indicada en cada columna (incluye "
                "mínimos, múltiplos y merma). En naranjo, cantidades para las que el stock actual de Mouser no alcanza.",
                _font(10), MUTED, after=6)
    columns = [Column("Parte", 2.6)] + [Column(fmt_int(q), 1.0, Qt.AlignRight) for q in report.quantities]
    rows = []
    for line in lines:
        row = [Cell(line.mpn, bold=True, sub=line.designators)]
        for qty, price, changed in zip(report.quantities, line.prices, line.changed):
            short = line.short_from is not None and qty >= line.short_from
            text = (fmt_price(price) + ("*" if changed else "")) if price is not None else "—"
            row.append(Cell(text, color=LEVEL_COLORS[LEVEL_WARN] if short and price is not None else None,
                            bold=qty == report.boards))
        rows.append(row)
    layout.table(columns, rows)


def _detail(layout: Layout, report: ClientReport) -> None:
    currency = report.currency
    layout.heading(f"Detalle de partes a {fmt_int(report.boards)} placas")
    columns = [Column("#", 0.35, Qt.AlignRight), Column("Designadores", 1.25), Column("Parte", 2.15),
               Column("Descripción", 1.75), Column("Por placa", 0.6, Qt.AlignRight),
               Column("A comprar", 0.7, Qt.AlignRight), Column(f"Precio unit. ({currency})", 0.85, Qt.AlignRight),
               Column(f"Total ({currency})", 0.85, Qt.AlignRight), Column("Estado", 1.05)]
    rows = []
    for line in report.lines:
        sub = line.manufacturer + (" · por especificación" if line.by_spec else "")
        rows.append([
            Cell(str(line.number)), Cell(line.designators), Cell(line.mpn, bold=True, sub=sub.strip(" ·")),
            Cell(line.description), Cell(fmt_int(line.qty_per_board)),
            Cell(fmt_int(line.buy_qty) if line.buy_qty else "—"),
            Cell(fmt_price(line.unit_price) if line.unit_price is not None else "—"),
            Cell(fmt_money(line.ext_price) if line.ext_price is not None else "—"),
            Cell(_status(line), color=_level_color(line.level), bold=line.level != LEVEL_OK),
        ])
    layout.table(columns, rows, font=_font(10), head_font=_font(10, bold=True), pad=3, after=6)
    summary = report.summary
    totals = [(f"Subtotal componentes ({fmt_int(summary.priced)} partes con precio)", fmt_money(summary.goods, currency))]
    if summary.freight:
        totals.append(("Flete estimado", fmt_money(summary.freight, currency)))
    if summary.duty:
        totals.append((f"Arancel ({report.params.duty_pct:g} %)", fmt_money(summary.duty, currency)))
    if summary.vat:
        totals.append((f"IVA ({report.params.vat_pct:g} %)", fmt_money(summary.vat, currency)))
    if len(totals) > 1:
        totals.append(("Total estimado", fmt_money(summary.total, currency)))
    if summary.total_clp is not None:
        totals.append(("Total estimado en pesos chilenos", f"CLP {fmt_int(summary.total_clp)}"))
    for label, value in totals:
        bold = len(totals) == 1 or label.startswith("Total estimado")
        layout.ensure(18)
        y = layout.y
        font = _font(11, bold=bold)

        def draw(p: QPainter, y=y, label=label, value=value, font=font) -> None:
            p.setFont(font)
            p.setPen(QColor(INK))
            p.drawText(QRectF(layout.left, y, layout.width - 130, 16), label, layout.option(Qt.AlignRight))
            p.drawText(QRectF(layout.left + layout.width - 120, y, 120, 16), value, layout.option(Qt.AlignRight))

        layout.add(draw)
        layout.space(17)
    layout.space(8)


def _notes(layout: Layout, report: ClientReport) -> None:
    layout.heading("Notas", keep=60)
    layout.bullets(report.notes, _font(10), MUTED)


def _decorate(painter: QPainter, layout: Layout, report: ClientReport, page: int, total: int) -> None:
    painter.save()
    small = _font(9)
    painter.setFont(small)
    metrics = QFontMetricsF(small)
    side = layout.width * 0.75  # textos de la izquierda: una línea, con «…» si no caben
    footer_y = layout.page_height - MARGIN_BOTTOM + 18
    painter.setPen(QPen(QColor(RULE), 0.6))
    painter.drawLine(QPointF(layout.left, footer_y - 6), QPointF(layout.left + layout.width, footer_y - 6))
    painter.setPen(QColor(MUTED))
    left = "Análisis de costo por volumen" + (f" · {report.client}" if report.client else "")
    painter.drawText(QRectF(layout.left, footer_y, side, 14), metrics.elidedText(left, Qt.ElideRight, side),
                     layout.option())
    painter.drawText(QRectF(layout.left, footer_y, layout.width, 14), f"Página {page} de {total}",
                     layout.option(Qt.AlignRight))
    if page > 1:
        header_y = MARGIN_TOP - 4
        title = metrics.elidedText(report.project or "Informe de costos", Qt.ElideRight, side)
        painter.drawText(QRectF(layout.left, header_y, side, 14), title, layout.option())
        painter.drawText(QRectF(layout.left, header_y, layout.width, 14),
                         report.generated_at.strftime("%d-%m-%Y"), layout.option(Qt.AlignRight))
        painter.setPen(QPen(QColor(RULE), 0.6))
        painter.drawLine(QPointF(layout.left, header_y + 18), QPointF(layout.left + layout.width, header_y + 18))
    painter.restore()


@dataclass
class ReportSections:
    top_parts: bool = True
    observations: bool = True
    price_matrix: bool = True
    detail: bool = True


def compose(layout: Layout, report: ClientReport, sections: ReportSections | None = None) -> None:
    sections = sections or ReportSections()
    _title_block(layout, report)
    _kpis(layout, report)
    _chart(layout, report)
    _scenario_table(layout, report)
    if sections.top_parts or sections.observations or sections.price_matrix or sections.detail:
        layout.new_page()
    if sections.top_parts:
        _top_parts(layout, report)
    if sections.observations:
        _observations(layout, report)
    if sections.price_matrix:
        _price_matrix(layout, report)
    if sections.detail:
        _detail(layout, report)
    _notes(layout, report)


def write_client_report(report: ClientReport, path: str | Path, page_size: str = "letter",
                        sections: ReportSections | None = None) -> int:
    """Escribe el PDF. Devuelve la cantidad de páginas."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(PAGE_SIZES.get(page_size, QPageSize.Letter)))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setResolution(1200)
    title = "Análisis de costo por volumen" + (f" – {report.project}" if report.project else "")
    writer.setTitle(title)
    writer.setCreator(f"MouserEngine {__version__}")
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError(f"No se pudo escribir {path}. ¿Está abierto en otro programa?")
    try:
        scale = writer.resolution() / 96
        page = writer.pageLayout().fullRectPixels(96)
        painter.scale(scale, scale)
        layout = Layout(painter, page.width(), page.height())
        compose(layout, report, sections)
        total = len(layout.pages)
        for index, ops in enumerate(layout.pages):
            if index:
                writer.newPage()
            painter.resetTransform()
            painter.scale(scale, scale)
            painter.setRenderHint(QPainter.Antialiasing)
            for op in ops:
                painter.save()
                op(painter)
                painter.restore()
            _decorate(painter, layout, report, index + 1, total)
    finally:
        painter.end()
    return total
