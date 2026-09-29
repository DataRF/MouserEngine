"""Gráfico de escenarios de volumen: costo por placa y costo total según la cantidad.

Vista superpuesta (solicitada): costo por placa en el eje izquierdo y costo total en el eje
derecho, sobre un mismo eje de cantidad en escala logarítmica. Vista separada: dos gráficos
alineados que comparten el eje de cantidad (más fácil de leer cuando las escalas difieren mucho).

La curva hasta la cantidad elegida con la barra se dibuja en color y el resto atenuado, de modo
que el gráfico "se va generando" al mover la barra. Al pasar el mouse se muestra la lectura de
esa cantidad y un clic la fija.
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


class ScenarioChart(QWidget):
    picked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[CurvePoint] = []
        self.currency = ""
        self.marks: list[int] = []
        self.max_boards = 1000
        self.cursor = 1
        self.cursor_point: CurvePoint | None = None
        self.hover: CurvePoint | None = None
        self.mode = "overlay"
        self.stale = False
        self.setMouseTracking(True)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

    # --- datos -----------------------------------------------------------------

    def set_data(self, points: list[CurvePoint], currency: str, marks: list[int], max_boards: int) -> None:
        self.points = points
        self.currency = currency
        self.marks = [m for m in marks if 1 <= m <= max_boards]
        self.max_boards = max(1, max_boards)
        self.stale = False
        self.update()

    def set_cursor(self, boards: int, point: CurvePoint | None) -> None:
        self.cursor = max(1, boards)
        self.cursor_point = point
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("overlay", "split") else "overlay"
        self.update()

    def set_stale(self, stale: bool) -> None:
        self.stale = stale
        self.update()

    # --- geometría ---------------------------------------------------------------

    def _plots(self) -> list[QRectF]:
        right = RIGHT_PAD_OVERLAY if self.mode == "overlay" else RIGHT_PAD_SPLIT
        area = QRectF(LEFT_PAD, TOP_PAD, max(10, self.width() - LEFT_PAD - right),
                      max(10, self.height() - TOP_PAD - BOTTOM_PAD))
        if self.mode == "overlay":
            return [area]
        half = (area.height() - GAP_SPLIT) / 2
        return [QRectF(area.left(), area.top(), area.width(), half),
                QRectF(area.left(), area.top() + half + GAP_SPLIT, area.width(), half)]

    def _x(self, boards: float, rect: QRectF) -> float:
        if self.max_boards <= 1:
            return rect.left()
        return rect.left() + math.log10(max(1.0, boards)) / math.log10(self.max_boards) * rect.width()

    def _boards_at(self, x: float, rect: QRectF) -> int:
        if self.max_boards <= 1 or rect.width() <= 0:
            return 1
        fraction = min(1.0, max(0.0, (x - rect.left()) / rect.width()))
        return max(1, min(self.max_boards, round(10 ** (fraction * math.log10(self.max_boards)))))

    def _nearest(self, boards: int) -> CurvePoint | None:
        if not self.points:
            return None
        return min(self.points, key=lambda p: (abs(math.log10(p.boards) - math.log10(max(1, boards))), p.boards))

    # --- eventos ----------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        rects = self._plots()
        pos = event.position()
        inside = any(r.adjusted(-4, -4, 4, 4).contains(pos) for r in rects)
        self.hover = self._nearest(self._boards_at(pos.x(), rects[0])) if inside and self.points else None
        self.update()

    def leaveEvent(self, event) -> None:
        self.hover = None
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.points:
            rects = self._plots()
            if any(r.adjusted(-4, -4, 4, 4).contains(event.position()) for r in rects):
                self.picked.emit(self._boards_at(event.position().x(), rects[0]))

    # --- dibujo -----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        if self.stale:
            painter.setOpacity(0.45)
        base_font = QFont(self.font())
        small = QFont(base_font)
        small.setPointSizeF(max(7.5, base_font.pointSizeF() - 1))
        if not self.points or not any(p.priced for p in self.points):
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "Consulte precios en Mouser para ver cómo cambia el costo con la cantidad.")
            return
        rects = self._plots()
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
            QFontMetrics(small), [(f"Costo por placa ({self.currency})", f"Por placa ({self.currency})"),
                                  (f"Costo total ({self.currency})", f"Total ({self.currency})")],
            rects[0].height())
        if self.mode == "overlay":
            rect = rects[0]
            self._draw_grid(painter, rect, small, unit_step, divisions, unit_title,
                            right_step=total_step, right_title=total_title)
            self._draw_x_axis(painter, rect, small)
            self._draw_series(painter, rect, [p.total for p in self.points], total_max, SERIES_2, area=False)
            self._draw_series(painter, rect, [p.total_unit for p in self.points], unit_max, SERIES_1, area=True)
            self._draw_cursor(painter, rects, unit_max, total_max, small)
        else:
            top, bottom = rects
            self._draw_grid(painter, top, small, unit_step, divisions, unit_title)
            self._draw_grid(painter, bottom, small, total_step, total_divisions, total_title)
            self._draw_x_axis(painter, bottom, small)
            self._draw_x_axis(painter, top, small, labels=False)
            self._draw_series(painter, top, [p.total_unit for p in self.points], unit_max, SERIES_1, area=True)
            self._draw_series(painter, bottom, [p.total for p in self.points], total_max, SERIES_2, area=True)
            self._draw_cursor(painter, rects, unit_max, total_max, small)
        painter.setOpacity(1.0)
        self._draw_readout(painter, rects[0], base_font, small)

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
            painter.translate(self.width() - 8, rect.center().y())
            painter.rotate(90)
            painter.drawText(QRectF(-rect.height() / 2, -8, rect.height(), 16), Qt.AlignCenter, right_title)
            painter.restore()

    def _draw_x_axis(self, painter: QPainter, rect: QRectF, font: QFont, labels: bool = True) -> None:
        painter.setFont(font)
        decades = [10 ** k for k in range(0, int(math.log10(self.max_boards)) + 1)]
        painter.setPen(QPen(QColor(GRIDLINE), 1))
        for value in sorted(set(decades) | set(self.marks)):
            x = self._x(value, rect)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        if not labels:
            return
        painter.setPen(QColor(MUTED))
        last_right = -1e9
        for value in sorted(set(self.marks) | {1, self.max_boards}):
            x = self._x(value, rect)
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
            pos = QPointF(self._x(point.boards, rect), rect.bottom() - value / maximum * rect.height())
            if not started:
                path.moveTo(pos)
                started = True
            else:
                path.lineTo(pos)
        return path

    def _draw_series(self, painter: QPainter, rect: QRectF, values: list[float], maximum: float,
                     color: str, area: bool) -> None:
        limit = self.cursor
        drawn = self._series_path(rect, values, maximum, limit, beyond=False)
        rest = self._series_path(rect, values, maximum, self._last_drawn_boards(limit), beyond=True)
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
        ahead = QColor(color)  # tramo a la derecha de la cantidad elegida: mismo color, atenuado
        ahead.setAlphaF(0.35)
        painter.setPen(QPen(ahead, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(rest)
        painter.setPen(QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(drawn)

    def _last_drawn_boards(self, limit: int) -> int:
        drawn = [p.boards for p in self.points if p.priced and p.boards <= limit]
        return drawn[-1] if drawn else 1

    def _draw_cursor(self, painter: QPainter, rects: list[QRectF], unit_max: float, total_max: float,
                     font: QFont) -> None:
        shown = self.hover or self.cursor_point
        if shown is None:
            return
        for index, rect in enumerate(rects):
            x = self._x(shown.boards, rect)
            painter.setPen(QPen(QColor(MUTED if self.hover else TEXT), 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        if self.mode == "overlay":
            rect = rects[0]
            dots = [(shown.total_unit / unit_max, SERIES_1), (shown.total / total_max, SERIES_2)]
            for fraction, color in dots:
                self._dot(painter, QPointF(self._x(shown.boards, rect), rect.bottom() - fraction * rect.height()), color)
        else:
            top, bottom = rects
            self._dot(painter, QPointF(self._x(shown.boards, top), top.bottom() - shown.total_unit / unit_max * top.height()),
                      SERIES_1)
            self._dot(painter, QPointF(self._x(shown.boards, bottom),
                                       bottom.bottom() - shown.total / total_max * bottom.height()), SERIES_2)

    @staticmethod
    def _dot(painter: QPainter, center: QPointF, color: str) -> None:
        painter.setPen(QPen(QColor(SURFACE), 2))
        painter.setBrush(QColor(color))
        painter.drawEllipse(center, 5, 5)
        painter.setBrush(Qt.NoBrush)

    def _draw_readout(self, painter: QPainter, rect: QRectF, font: QFont, small: QFont) -> None:
        shown = self.hover or self.cursor_point
        if shown is None:
            return
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
        x_anchor = self._x(shown.boards, rect)
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
