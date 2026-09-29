"""Genera el ícono de la aplicación (PNG e ICO) con Qt.

Uso: python tools/make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPen  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "mouser_engine" / "assets"


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0
    gradient = QLinearGradient(QPointF(0, 0), QPointF(0, size))
    gradient.setColorAt(0, QColor("#2B7BD6"))
    gradient.setColorAt(1, QColor("#17457F"))
    p.setBrush(gradient)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 48 * s, 48 * s)

    # patas del chip
    p.setBrush(QColor("#FFFFFF"))
    pin_w, pin_l = 14 * s, 22 * s
    for i in range(4):
        offset = (78 + i * 34) * s
        p.drawRoundedRect(QRectF(offset - pin_w / 2, 36 * s, pin_w, pin_l), 3 * s, 3 * s)
        p.drawRoundedRect(QRectF(offset - pin_w / 2, 198 * s, pin_w, pin_l), 3 * s, 3 * s)
        p.drawRoundedRect(QRectF(36 * s, offset - pin_w / 2, pin_l, pin_w), 3 * s, 3 * s)
        p.drawRoundedRect(QRectF(198 * s, offset - pin_w / 2, pin_l, pin_w), 3 * s, 3 * s)

    # cuerpo del chip
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(QRectF(56 * s, 56 * s, 144 * s, 144 * s), 20 * s, 20 * s)
    p.setBrush(QColor("#1F5FAD"))
    p.drawEllipse(QRectF(70 * s, 70 * s, 14 * s, 14 * s))

    # signo $
    font = QFont("DejaVu Sans")
    font.setBold(True)
    font.setPixelSize(max(6, int(112 * s)))
    p.setFont(font)
    p.setPen(QPen(QColor("#17457F")))
    p.drawText(QRectF(56 * s, 52 * s, 144 * s, 150 * s), Qt.AlignCenter, "$")
    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    """ICO con imágenes PNG embebidas (formato soportado desde Windows Vista)."""
    images = [(size, png_bytes(draw(size))) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    path.write_bytes(header + entries + payload)


def main() -> int:
    QGuiApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    draw(256).save(str(OUT / "icon.png"))
    write_ico(OUT / "icon.ico", [16, 24, 32, 48, 64, 128, 256])
    print(f"Íconos generados en {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
