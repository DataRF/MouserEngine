"""Opciones del informe PDF para el cliente."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..config import DEFAULT_SCENARIOS, parse_quantities
from .pdf_report import PAGE_LABELS, ReportSections


@dataclass
class ReportOptions:
    client: str = ""
    project: str = ""
    boards: int = 1
    quantities: list[int] = field(default_factory=lambda: parse_quantities(DEFAULT_SCENARIOS))
    max_boards: int = 1000
    chart_mode: str = "overlay"
    page_size: str = "letter"
    sections: ReportSections = field(default_factory=ReportSections)


class ClientReportDialog(QDialog):
    def __init__(self, options: ReportOptions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Informe PDF para el cliente")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel("Informe genérico, sin la plantilla de la empresa, con el análisis comercial del costo de los "
                       "componentes según la cantidad a fabricar (no incluye el BOM). Usa los precios y el stock de la "
                       "última consulta a Mouser y, si está activo, el precio con todo incluido (puesto en Chile).")
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.client_edit = QLineEdit(options.client)
        self.client_edit.setPlaceholderText("Empresa para la que se diseña")
        form.addRow("Cliente:", self.client_edit)
        self.project_edit = QLineEdit(options.project)
        self.project_edit.setPlaceholderText("Nombre del proyecto o de la placa")
        form.addRow("Proyecto:", self.project_edit)
        self.boards_spin = QSpinBox()
        self.boards_spin.setRange(1, 10_000_000)
        self.boards_spin.setGroupSeparatorShown(True)
        self.boards_spin.setSuffix(" placas")
        self.boards_spin.setValue(max(1, options.boards))
        self.boards_spin.setToolTip("Cantidad para el resumen de costos")
        form.addRow("Cantidad de referencia:", self.boards_spin)
        self.quantities_edit = QLineEdit(", ".join(str(q) for q in options.quantities))
        self.quantities_edit.setToolTip("Cantidades de placas separadas por coma")
        form.addRow("Cantidades a comparar:", self.quantities_edit)
        self.max_spin = QSpinBox()
        self.max_spin.setRange(10, 1_000_000)
        self.max_spin.setGroupSeparatorShown(True)
        self.max_spin.setSuffix(" placas")
        self.max_spin.setValue(max(10, options.max_boards))
        form.addRow("Gráfico hasta:", self.max_spin)
        self.chart_combo = QComboBox()
        self.chart_combo.addItem("Superpuesto (dos ejes)", "overlay")
        self.chart_combo.addItem("Separado (dos gráficos)", "split")
        self.chart_combo.setCurrentIndex(1 if options.chart_mode == "split" else 0)
        form.addRow("Gráfico:", self.chart_combo)
        self.page_combo = QComboBox()
        for key, label in PAGE_LABELS.items():
            self.page_combo.addItem(label, key)
        self.page_combo.setCurrentIndex(max(0, self.page_combo.findData(options.page_size)))
        form.addRow("Tamaño de hoja:", self.page_combo)
        layout.addLayout(form)

        group = QGroupBox("Incluir además del resumen, el gráfico, los escenarios y los costos")
        group_layout = QVBoxLayout(group)
        sections = options.sections
        self.top_check = QCheckBox("Partes que más influyen en el costo")
        self.top_check.setChecked(sections.top_parts)
        self.observations_check = QCheckBox("Observaciones (stock, ciclo de vida, partes sin precio)")
        self.observations_check.setChecked(sections.observations)
        for check in (self.top_check, self.observations_check):
            group_layout.addWidget(check)
        layout.addWidget(group)

        buttons = QDialogButtonBox()
        generate = buttons.addButton("Generar PDF…", QDialogButtonBox.AcceptRole)
        generate.setObjectName("Primary")
        generate.setDefault(True)
        buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> ReportOptions:
        quantities = parse_quantities(self.quantities_edit.text()) or parse_quantities(DEFAULT_SCENARIOS)
        return ReportOptions(
            client=self.client_edit.text().strip(),
            project=self.project_edit.text().strip(),
            boards=self.boards_spin.value(),
            quantities=quantities,
            max_boards=max(self.max_spin.value(), max(quantities)),
            chart_mode=self.chart_combo.currentData(),
            page_size=self.page_combo.currentData(),
            sections=ReportSections(self.top_check.isChecked(), self.observations_check.isChecked()),
        )
