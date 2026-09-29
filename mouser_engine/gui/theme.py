"""Paleta y hoja de estilos de la aplicación (tema claro consistente en todos los sistemas)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from ..models import LEVEL_ERROR, LEVEL_EXCLUDED, LEVEL_OK, LEVEL_PENDING, LEVEL_WARN

ACCENT = "#1F5FAD"
TEXT = "#1F2328"
MUTED = "#59636E"
BORDER = "#D1D9E0"
SURFACE = "#FFFFFF"
WINDOW = "#F3F5F8"
INDICATOR_BORDER = "#8C959F"
ASSETS = Path(__file__).resolve().parents[1] / "assets"

# Gráficos: paleta categórica validada (azul / naranja) y cromo neutro.
SERIES_1 = "#2A78D6"   # costo por placa
SERIES_2 = "#EB6834"   # costo total
GRIDLINE = "#E6EAEF"
AXIS = "#C9D1D9"

# Fondo de fila y color del indicador de estado.
ROW_BACKGROUND = {
    LEVEL_OK: None,
    LEVEL_WARN: "#FFF7DB",
    LEVEL_ERROR: "#FDE8E8",
    LEVEL_EXCLUDED: "#F1F1F1",
    LEVEL_PENDING: None,
}
STATUS_COLOR = {
    LEVEL_OK: "#1A7F37",
    LEVEL_WARN: "#9A6700",
    LEVEL_ERROR: "#CF222E",
    LEVEL_EXCLUDED: "#6E7781",
    LEVEL_PENDING: "#6E7781",
}

_BASE_STYLESHEET = f"""
QMainWindow, QDialog {{ background: {WINDOW}; }}
QToolBar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; padding: 4px; spacing: 4px; }}
QToolBar QToolButton {{ padding: 5px 9px; border-radius: 6px; color: {TEXT}; }}
QToolBar QToolButton:hover {{ background: #EAF1FB; }}
QToolBar QToolButton:disabled {{ color: #A0A7AE; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel#CardTitle {{ color: {MUTED}; font-size: 9pt; }}
QLabel#CardValue {{ color: {TEXT}; font-size: 17pt; font-weight: 600; }}
QLabel#CardSub {{ color: {MUTED}; font-size: 8.5pt; }}
QLabel#SectionTitle {{ color: {TEXT}; font-weight: 600; font-size: 10pt; }}
QLabel#Hint {{ color: {MUTED}; }}
QLabel#Banner {{ background: #FFF4CE; border: 1px solid #E8D48A; border-radius: 6px; padding: 6px 10px; color: #5C4B00; }}
QLabel#ErrorBanner {{ background: #FDE8E8; border: 1px solid #F0B4B4; border-radius: 6px; padding: 6px 10px; color: #8A1C1C; }}
QGroupBox {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT}; }}
QTableView, QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    gridline-color: #E6EAEF; selection-background-color: #CFE3FA; selection-color: {TEXT};
    alternate-background-color: #F8FAFC; }}
QHeaderView::section {{ background: #EEF2F6; color: {TEXT}; padding: 5px 6px; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; background: {SURFACE}; top: -1px; }}
QTabBar::tab {{ padding: 6px 14px; border: 1px solid transparent; }}
QTabBar::tab:selected {{ background: {SURFACE}; border: 1px solid {BORDER}; border-bottom-color: {SURFACE};
    border-top-left-radius: 6px; border-top-right-radius: 6px; font-weight: 600; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 6px; background: {SURFACE}; }}
QPushButton:hover {{ background: #EEF4FB; }}
QPushButton:disabled {{ color: #A0A7AE; }}
QPushButton#Primary {{ background: {ACCENT}; color: white; border: 1px solid {ACCENT}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #184F92; }}
QPushButton#Primary:disabled {{ background: #9DB7D8; border-color: #9DB7D8; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QTextBrowser {{ border: none; background: {SURFACE}; }}
QFrame#DropZone {{ background: {SURFACE}; border: 2px dashed #AFC2D8; border-radius: 12px; }}
QCheckBox {{ spacing: 7px; }}
"""


def _indicator_stylesheet() -> str:
    """Casillas dibujadas explícitamente: en Windows el borde de Fusion puede quedar invisible."""
    check = (ASSETS / "check.png").as_posix()
    rules = []
    for selector in ("QCheckBox::indicator", "QTableView::indicator", "QTableWidget::indicator"):
        rules.append(f"""
{selector} {{ width: 14px; height: 14px; border: 1px solid {INDICATOR_BORDER}; border-radius: 3px;
    background: {SURFACE}; }}
{selector}:hover {{ border-color: {ACCENT}; }}
{selector}:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("{check}"); }}
{selector}:disabled {{ background: #EEF1F4; border-color: #C9D1D9; }}
{selector}:checked:disabled {{ background: #9DB7D8; border-color: #9DB7D8; }}""")
    return "".join(rules)


def build_stylesheet() -> str:
    return _BASE_STYLESHEET + _indicator_stylesheet()


STYLESHEET = build_stylesheet()


def apply_theme(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(WINDOW))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(SURFACE))
    palette.setColor(QPalette.AlternateBase, QColor("#F8FAFC"))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(SURFACE))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor("#CFE3FA"))
    palette.setColor(QPalette.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.ToolTipBase, QColor("#FFFFFF"))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.Link, QColor(ACCENT))
    palette.setColor(QPalette.PlaceholderText, QColor("#8C959F"))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor("#A0A7AE"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
