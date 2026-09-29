"""Gráfico de escenarios de volumen: costo por placa y costo total según la cantidad.

Vista superpuesta (solicitada): costo por placa en el eje izquierdo y costo total en el eje
derecho, sobre un mismo eje de cantidad en escala logarítmica. Vista separada: dos gráficos
alineados que comparten el eje de cantidad (más fácil de leer cuando las escalas difieren mucho).

En pantalla, la curva hasta la cantidad elegida con la barra se dibuja en color y el resto
atenuado, de modo que el gráfico "se va generando" al mover la barra. Al pasar el mouse se
muestra la lectura de esa cantidad y un clic la fija.

El dibujo está en `ChartPainter`, que no depende del widget: el informe PDF usa el mismo código
(escalando el QPainter), así el gráfico impreso es idéntico al de la aplicación.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..formatting import fmt_board_cost, fmt_int, fmt_num
from ..scenarios import CurvePoint
from .theme import AXIS, GRIDLINE, MUTED, SERIES_1, SERIES_2, SURFACE, TEXT

LEFT_PAD = 78
RIGHT_PAD_OVERLAY = 84
RIGHT_PAD_SPLIT = 24
TOP_PAD = 34
BOTTOM_PAD = 46
GAP_SPLIT = 28


def nice_step(raw: float) -> float:
    """Menor paso "redondo" (1, 2, 2,5 o 5 × 10^k) que no es menor que raw."""
    if raw <= 0:
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = 10 ** exponent
    for step in (1, 2, 2.5, 5, 10):
        if raw <= step * base * (1 + 1e-9):
            return step * base
    return 10 * base


def axis_scale(data_max: float, divisions: int = 5) -> tuple[float, int]:
    """(paso, cantidad de divisiones) para un eje de 0 a paso × divisiones que cubre data_max."""
    step = nice_step(max(data_max, 1e-9) / divisions)
    count = max(1, math.ceil(data_max / step - 1e-9))
    return step, count


def fmt_axis(value: float, maximum: float) -> str:
    if maximum < 1:
        return fmt_num(value, 2, 3)
    if maximum < 20:
        return fmt_num(value, 0, 2)
    return fmt_int(round(value))


class ChartPainter:
    """Dibuja el gráfico en cualquier QPainter, en coordenadas de pantalla (96 ppp)."""

    def __init__(self) -> None:
        self.points: list[CurvePoint] = []
        self.currency = ""
        self.marks: list[int] = []
        self.max_boards = 1000
        self.mode = "overlay"
        self._width = 0.0

    def set_data(self, points: list[CurvePoint], currency: str, marks: list[int], max_boards: int) -> None:
        self.points = points
        self.currency = currency
        self.max_boards = max(1, int(max_boards))
        self.marks = [m for m in marks if 1 <= m <= self.max_boards]

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("overlay", "split") else "overlay"

    @property
    def has_data(self) -> bool:
        return any(p.priced for p in self.points)

    # --- geometría ---------------------------------------------------------------

    def plots(self, width: float, height: float) -> list[QRectF]:
        right = RIGHT_PAD_OVERLAY if self.mode == "overlay" else RIGHT_PAD_SPLIT
        area = QRectF(LEFT_PAD, TOP_PAD, max(10, width - LEFT_PAD - right), max(10, height - TOP_PAD - BOTTOM_PAD))
        if self.mode == "overlay":
            return [area]
        half = (area.height() - GAP_SPLIT) / 2
        return [QRectF(area.left(), area.top(), area.width(), half),
                QRectF(area.left(), area.top() + half + GAP_SPLIT, area.width(), half)]

    def x_of(self, boards: float, rect: QRectF) -> float:
        if self.max_boards <= 1:
            return rect.left()
        return rect.left() + math.log10(max(1.0, boards)) / math.log10(self.max_boards) * rect.width()

    def boards_at(self, x: float, rect: QRectF) -> int:
        if self.max_boards <= 1 or rect.width() <= 0:
            return 1
        fraction = min(1.0, max(0.0, (x - rect.left()) / rect.width()))
        return max(1, min(self.max_boards, round(10 ** (fraction * math.log10(self.max_boards)))))

    def nearest(self, boards: int) -> CurvePoint | None:
        if not self.points:
            return None
        return min(self.points, key=lambda p: (abs(math.log10(p.boards) - math.log10(max(1, boards))), p.boards))

    def point_at(self, boards: int) -> CurvePoint | None:
        """El punto calculado para esa cantidad exacta (las cantidades comparadas siempre están)."""
        return next((p for p in self.points if p.boards == boards), None)

    # --- dibujo -----------------------------------------------------------------

    def paint(self, painter: QPainter, width: float, height: float, base_font: QFont, small_font: QFont, *,
              cursor: int | None = None, shown: CurvePoint | None = None, hovering: bool = False,
              readout: bool = True, markers: bool = False) -> None:
        """Dibuja el gráfico en el rectángulo (0, 0, width, height).

        - `cursor`: la curva se dibuja en color hasta esa cantidad y atenuada después (pantalla);
          None la dibuja completa.
        - `shown`: cantidad destacada con línea vertical, puntos y (si `readout`) el recuadro de lectura.
        - `markers`: puntos en cada cantidad comparada (informe impreso).
        """
        self._width = width
        rects = self.plots(width, height)
        unit_data = max(p.total_unit for p in self.points if p.priced) * 1.04
        total_data = max(p.total for p in self.points if p.priced) * 1.04
        unit_step, divisions = axis_scale(unit_data)
        unit_max = unit_step * divisions
        if self.mode == "overlay":
            total_step = nice_step(total_data / divisions)
            total_divisions = divisions
        else:
            total_step, total_divisions = axis_scale(total_data)
        total_max = total_step * total_divisions
        self._draw_legend(painter, base_font)
        unit_title, total_title = self._axis_titles(
            QFontMetrics(small_font), [(f"Costo por placa ({self.currency})", f"Por placa ({self.currency})"),
                                       (f"Costo total ({self.currency})", f"Total ({self.currency})")],
            rects[0].height())
        unit_values = [p.total_unit for p in self.points]
        total_values = [p.total for p in self.points]
        if self.mode == "overlay":
            rect = rects[0]
            self._draw_grid(painter, rect, small_font, unit_step, divisions, unit_title,
                            right_step=total_step, right_title=total_title)
            self._draw_x_axis(painter, rect, small_font)
            self._draw_series(painter, rect, total_values, total_max, SERIES_2, area=False, cursor=cursor)
            self._draw_series(painter, rect, unit_values, unit_max, SERIES_1, area=True, cursor=cursor)
            if markers:
                self._draw_markers(painter, rect, total_values, total_max, SERIES_2)
                self._draw_markers(painter, rect, unit_values, unit_max, SERIES_1)
        else:
            top, bottom = rects
            self._draw_grid(painter, top, small_font, unit_step, divisions, unit_title)
            self._draw_grid(painter, bottom, small_font, total_step, total_divisions, total_title)
            self._draw_x_axis(painter, bottom, small_font)
            self._draw_x_axis(painter, top, small_font, labels=False)
            self._draw_series(painter, top, unit_values, unit_max, SERIES_1, area=True, cursor=cursor)
            self._draw_series(painter, bottom, total_values, total_max, SERIES_2, area=True, cursor=cursor)
            if markers:
                self._draw_markers(painter, top, unit_values, unit_max, SERIES_1)
                self._draw_markers(painter, bottom, total_values, total_max, SERIES_2)
        if shown is not None:
            self._draw_cursor(painter, rects, unit_max, total_max, shown, hovering)
        painter.setOpacity(1.0)
        if shown is not None and readout:
            self._draw_readout(painter, rects[0], base_font, small_font, shown)

    def _draw_legend(self, painter: QPainter, font: QFont) -> None:
        painter.setFont(font)
        metrics = QFontMetrics(font)
        x = LEFT_PAD
        y = 14
        entries = [(SERIES_1, "Costo por placa" + (" (eje izquierdo)" if self.mode == "overlay" else "")),
                   (SERIES_2, "Costo total" + (" (eje derecho)" if self.mode == "overlay" else ""))]
        for color, label in entries:
            painter.setPen(QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(x, y), QPointF(x + 18, y))
            painter.setPen(QColor(TEXT))
            painter.drawText(QPointF(x + 24, y + metrics.ascent() / 2 - 1), label)
            x += 24 + metrics.horizontalAdvance(label) + 22

    @staticmethod
    def _axis_titles(metrics: QFontMetrics, titles: list[tuple[str, str]], length: float) -> list[str]:
        """Títulos de los ejes (completos o cortos, todos iguales) que caben a lo alto del gráfico."""
        if all(metrics.horizontalAdvance(full) <= length for full, _ in titles):
            return [full for full, _ in titles]
        return [metrics.elidedText(short, Qt.ElideRight, int(length)) for _, short in titles]

    def _draw_grid(self, painter: QPainter, rect: QRectF, font: QFont, step: float, divisions: int,
                   left_title: str, right_step: float | None = None, right_title: str = "") -> None:
        painter.setFont(font)
        left_max = step * divisions
        for i in range(divisions + 1):
            y = rect.bottom() - i / divisions * rect.height()
            painter.setPen(QPen(QColor(GRIDLINE if i else AXIS), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(rect.left() - 70, y - 8, 64, 16), Qt.AlignRight | Qt.AlignVCenter,
                             fmt_axis(i * step, left_max))
            if right_step is not None:
                painter.drawText(QRectF(rect.right() + 6, y - 8, 76, 16), Qt.AlignLeft | Qt.AlignVCenter,
                                 fmt_axis(i * right_step, right_step * divisions))
        painter.save()
        painter.setPen(QColor(MUTED))
        painter.translate(12, rect.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-rect.height() / 2, -8, rect.height(), 16), Qt.AlignCenter, left_title)
        painter.restore()
        if right_step is not None and right_title:
            painter.save()
            painter.translate(self._width - 8, rect.center().y())
            painter.rotate(90)
            painter.drawText(QRectF(-rect.height() / 2, -8, rect.height(), 16), Qt.AlignCenter, right_title)
            painter.restore()

    def _draw_x_axis(self, painter: QPainter, rect: QRectF, font: QFont, labels: bool = True) -> None:
        painter.setFont(font)
        decades = [10 ** k for k in range(0, int(math.log10(self.max_boards)) + 1)]
        painter.setPen(QPen(QColor(GRIDLINE), 1))
        for value in sorted(set(decades) | set(self.marks)):
            x = self.x_of(value, rect)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        if not labels:
            return
        painter.setPen(QColor(MUTED))
        last_right = -1e9
        for value in sorted(set(self.marks) | {1, self.max_boards}):
            x = self.x_of(value, rect)
            text = fmt_int(value)
            width = QFontMetrics(font).horizontalAdvance(text)
            if x - width / 2 < last_right + 4:
                continue
            painter.drawText(QRectF(x - 40, rect.bottom() + 4, 80, 16), Qt.AlignHCenter | Qt.AlignTop, text)
            last_right = x + width / 2
        painter.drawText(QRectF(rect.left(), rect.bottom() + 22, rect.width(), 16), Qt.AlignCenter,
                         "Placas / equipos (escala logarítmica)")

    def _series_path(self, rect: QRectF, values: list[float], maximum: float,
                     limit: int | None, beyond: bool) -> QPainterPath:
        path = QPainterPath()
        started = False
        for point, value in zip(self.points, values):
            if not point.priced:
                continue
            if limit is not None:
                if not beyond and point.boards > limit:
                    break
                if beyond and point.boards < limit:
                    continue
            pos = QPointF(self.x_of(point.boards, rect), rect.bottom() - value / maximum * rect.height())
            if not started:
                path.moveTo(pos)
                started = True
            else:
                path.lineTo(pos)
        return path

    def _draw_series(self, painter: QPainter, rect: QRectF, values: list[float], maximum: float,
                     color: str, area: bool, cursor: int | None) -> None:
        drawn = self._series_path(rect, values, maximum, cursor, beyond=False)
        if area and not drawn.isEmpty():
            fill = QPainterPath(drawn)
            last = drawn.currentPosition()
            fill.lineTo(QPointF(last.x(), rect.bottom()))
            first = drawn.elementAt(0)
            fill.lineTo(QPointF(first.x, rect.bottom()))
            fill.closeSubpath()
            wash = QColor(color)
            wash.setAlphaF(0.10)
            painter.fillPath(fill, wash)
        if cursor is not None:
            rest = self._series_path(rect, values, maximum, self._last_drawn_boards(cursor), beyond=True)
            ahead = QColor(color)  # tramo a la derecha de la cantidad elegida: mismo color, atenuado
            ahead.setAlphaF(0.35)
            painter.setPen(QPen(ahead, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(rest)
        painter.setPen(QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(drawn)

    def _draw_markers(self, painter: QPainter, rect: QRectF, values: list[float], maximum: float, color: str) -> None:
        wanted = set(self.marks)
        for point, value in zip(self.points, values):
            if point.priced and point.boards in wanted:
                self._dot(painter, QPointF(self.x_of(point.boards, rect), rect.bottom() - value / maximum * rect.height()),
                          color, radius=4)

    def _last_drawn_boards(self, limit: int) -> int:
        drawn = [p.boards for p in self.points if p.priced and p.boards <= limit]
        return drawn[-1] if drawn else 1

    def _draw_cursor(self, painter: QPainter, rects: list[QRectF], unit_max: float, total_max: float,
                     shown: CurvePoint, hovering: bool) -> None:
        for rect in rects:
            x = self.x_of(shown.boards, rect)
            painter.setPen(QPen(QColor(MUTED if hovering else TEXT), 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        if self.mode == "overlay":
            rect = rects[0]
            dots = [(shown.total_unit / unit_max, SERIES_1), (shown.total / total_max, SERIES_2)]
            for fraction, color in dots:
                self._dot(painter, QPointF(self.x_of(shown.boards, rect), rect.bottom() - fraction * rect.height()),
                          color)
        else:
            top, bottom = rects
            self._dot(painter, QPointF(self.x_of(shown.boards, top),
                                       top.bottom() - shown.total_unit / unit_max * top.height()), SERIES_1)
            self._dot(painter, QPointF(self.x_of(shown.boards, bottom),
                                       bottom.bottom() - shown.total / total_max * bottom.height()), SERIES_2)

    @staticmethod
    def _dot(painter: QPainter, center: QPointF, color: str, radius: float = 5) -> None:
        painter.setPen(QPen(QColor(SURFACE), 2))
        painter.setBrush(QColor(color))
        painter.drawEllipse(center, radius, radius)
        painter.setBrush(Qt.NoBrush)

    def _draw_readout(self, painter: QPainter, rect: QRectF, font: QFont, small: QFont, shown: CurvePoint) -> None:
        bold = QFont(font)
        bold.setBold(True)
        metrics = QFontMetrics(font)
        bold_metrics = QFontMetrics(bold)
        title = f"{fmt_int(shown.boards)} {'placa' if shown.boards == 1 else 'placas'}"
        rows = [(SERIES_1, fmt_board_cost(shown.total_unit), "por placa"),
                (SERIES_2, fmt_num(shown.total, 2), "total")]
        notes = []
        if shown.short:
            notes.append(f"{shown.short} {'parte' if shown.short == 1 else 'partes'} sin stock suficiente")
        if shown.unpriced:
            notes.append(f"{shown.unpriced} sin precio (no {'suma' if shown.unpriced == 1 else 'suman'})")
        width = max([bold_metrics.horizontalAdvance(title)]
                    + [26 + bold_metrics.horizontalAdvance(f"{self.currency} {v}") + 8
                       + metrics.horizontalAdvance(label) for _, v, label in rows]
                    + [QFontMetrics(small).horizontalAdvance(n) for n in notes]) + 20
        height = 12 + bold_metrics.height() + len(rows) * (metrics.height() + 3) + len(notes) * (
            QFontMetrics(small).height() + 1) + 8
        x_anchor = self.x_of(shown.boards, rect)
        left = x_anchor + 12 if x_anchor + 12 + width < rect.right() else x_anchor - 12 - width
        box = QRectF(max(rect.left() + 4, left), rect.top() + 6, width, height)
        painter.setPen(QPen(QColor(GRIDLINE), 1))
        painter.setBrush(QColor(255, 255, 255, 240))
        painter.drawRoundedRect(box, 6, 6)
        painter.setBrush(Qt.NoBrush)
        y = box.top() + 8 + bold_metrics.ascent()
        painter.setFont(bold)
        painter.setPen(QColor(TEXT))
        painter.drawText(QPointF(box.left() + 10, y), title)
        y += 6
        for color, value, label in rows:
            y += metrics.height() + 3
            painter.setPen(QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(box.left() + 10, y - metrics.ascent() / 2 + 1),
                             QPointF(box.left() + 26, y - metrics.ascent() / 2 + 1))
            painter.setFont(bold)
            painter.setPen(QColor(TEXT))
            text = f"{self.currency} {value}".strip()
            painter.drawText(QPointF(box.left() + 32, y), text)
            painter.setFont(font)
            painter.setPen(QColor(MUTED))
            painter.drawText(QPointF(box.left() + 32 + bold_metrics.horizontalAdvance(text) + 8, y), label)
        painter.setFont(small)
        for note in notes:
            y += QFontMetrics(small).height() + 1
            painter.setPen(QColor("#9A6700"))
            painter.drawText(QPointF(box.left() + 10, y), note)


class ScenarioChart(QWidget):
    """El gráfico en pantalla: barra de cantidad, lectura al pasar el mouse y clic para elegir."""

    picked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.renderer = ChartPainter()
        self.cursor = 1
        self.cursor_point: CurvePoint | None = None
        self.hover: CurvePoint | None = None
        self.stale = False
        self.setMouseTracking(True)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

    @property
    def points(self) -> list[CurvePoint]:
        return self.renderer.points

    @property
    def mode(self) -> str:
        return self.renderer.mode

    # --- datos -----------------------------------------------------------------

    def set_data(self, points: list[CurvePoint], currency: str, marks: list[int], max_boards: int) -> None:
        self.renderer.set_data(points, currency, marks, max_boards)
        self.stale = False
        self.update()

    def set_cursor(self, boards: int, point: CurvePoint | None) -> None:
        self.cursor = max(1, boards)
        self.cursor_point = point
        self.update()

    def set_mode(self, mode: str) -> None:
        self.renderer.set_mode(mode)
        self.update()

    def set_stale(self, stale: bool) -> None:
        self.stale = stale
        self.update()

    # --- eventos ----------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        rects = self.renderer.plots(self.width(), self.height())
        pos = event.position()
        inside = any(r.adjusted(-4, -4, 4, 4).contains(pos) for r in rects)
        self.hover = (self.renderer.nearest(self.renderer.boards_at(pos.x(), rects[0]))
                      if inside and self.points else None)
        self.update()

    def leaveEvent(self, event) -> None:
        self.hover = None
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.points:
            rects = self.renderer.plots(self.width(), self.height())
            if any(r.adjusted(-4, -4, 4, 4).contains(event.position()) for r in rects):
                self.picked.emit(self.renderer.boards_at(event.position().x(), rects[0]))

    # --- dibujo -----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        if not self.renderer.has_data:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "Consulte precios en Mouser para ver cómo cambia el costo con la cantidad.")
            return
        if self.stale:
            painter.setOpacity(0.45)
        base_font = QFont(self.font())
        small = QFont(base_font)
        small.setPointSizeF(max(7.5, base_font.pointSizeF() - 1))
        self.renderer.paint(painter, self.width(), self.height(), base_font, small, cursor=self.cursor,
                            shown=self.hover or self.cursor_point, hovering=self.hover is not None)
