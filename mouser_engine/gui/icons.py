"""Íconos dibujados en tiempo de ejecución (sin archivos adicionales)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .theme import TEXT


def cart_icon(color: str = TEXT, size: int = 64) -> QIcon:
    """Carro de compras simple (manilla, canasto y dos ruedas)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(size / 64, size / 64)
    pen = QPen(QColor(color), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    handle = QPainterPath(QPointF(4, 10))
    handle.lineTo(13, 10)
    handle.lineTo(22, 44)
    handle.lineTo(52, 44)
    painter.drawPath(handle)
    basket = QPainterPath(QPointF(15, 18))
    basket.lineTo(59, 18)
    basket.lineTo(53, 36)
    basket.lineTo(20, 36)
    basket.closeSubpath()
    painter.drawPath(basket)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    for x in (26, 49):
        painter.drawEllipse(QRectF(x - 5, 49, 10, 10))
    painter.end()
    return QIcon(pixmap)
