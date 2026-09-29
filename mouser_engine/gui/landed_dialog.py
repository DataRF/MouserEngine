"""Desglose del precio con todo incluido (puesto en Chile): lo que cobra Mouser y lo que cobra DHL."""

from __future__ import annotations

from datetime import datetime
from html import escape

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from ..formatting import fmt_int, fmt_money, fmt_num
from ..landed import ImportSetup, LandedCost, month_name


def _day(text: str) -> str:
    """"2026-09-29" -> "29-09-2026"."""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return text


def rates_text(setup: ImportSetup) -> str:
    """Los tipos de cambio usados, con su fecha y su origen."""
    rates = setup.rates
    if setup.estimated_rates:
        return (f"Sin conexión: se usa un dólar de referencia de {fmt_num(setup.usd_rate, 2)} CLP hasta obtener el "
                "del día.")
    parts = []
    if rates.usd:
        parts.append(f"dólar observado {fmt_num(rates.usd, 2)} CLP" + (f" del {_day(rates.usd_date)}"
                                                                        if rates.usd_date else ""))
    if rates.customs:
        customs = f"dólar aduanero {fmt_num(rates.customs, 2)} CLP"
        if rates.customs_month:
            customs += f" de {month_name(rates.customs_month)}"
        if rates.customs_date:
            customs += f" (observado del {_day(rates.customs_date)}, penúltimo día hábil del mes anterior)"
        parts.append(customs)
    return "Tipo de cambio: " + "; ".join(parts) + ". Fuente: Banco Central de Chile, vía mindicador.cl."


def rules_text(setup: ImportSetup) -> str:
    rules = setup.rules
    origin = {"incluidas": "incluidas en el programa", "descargadas": "descargadas del repositorio",
              "guardadas": "guardadas"}.get(rules.source, rules.source)
    return f"Reglas de importación del {_day(rules.version)} ({origin})."


def landed_html(cost: LandedCost, boards: int) -> str:
    """Tabla HTML con el desglose, en dólares (como la factura de DHL) y en pesos."""
    rules = cost.setup.rules
    clp_account = cost.currency == "CLP"
    usd = lambda value: fmt_num(value, 2)  # noqa: E731
    clp = lambda value: fmt_int(value)  # noqa: E731
    vat = fmt_num(rules.vat_pct, 0, 2)
    rows: list[str] = []

    def section(title: str) -> None:
        rows.append(f"<tr><td colspan='3' style='padding-top:10px; color:#1F3A5F'><b>{escape(title)}</b></td></tr>")

    def row(label: str, in_usd: str = "", in_clp: str = "", bold: bool = False, muted: bool = False) -> None:
        style = " style='color:#59636E'" if muted else ""
        wrap = (lambda text: f"<b>{text}</b>") if bold else (lambda text: text)
        rows.append(f"<tr{style}><td>{wrap(escape(label))}</td><td align='right'>{wrap(in_usd)}</td>"
                    f"<td align='right'>{wrap(in_clp)}</td></tr>")

    section("Compra en Mouser")
    row("Componentes", usd(cost.fob_usd), clp(cost.to_clp(cost.goods)))
    row("Flete de Mouser (DHL Express)", usd(cost.freight_usd), clp(cost.to_clp(cost.freight)))
    row("Subtotal Mouser", usd(cost.fob_usd + cost.freight_usd), clp(cost.to_clp(cost.mouser_total)), bold=True)
    section("Importación: lo que cobra DHL al entregar (al dólar aduanero)")
    row("Valor FOB", usd(cost.fob_usd), muted=True)
    row("Flete declarado en aduana", usd(cost.declared_freight_usd), muted=True)
    row(f"Seguro ({fmt_num(rules.insurance_pct, 0, 2)} % del FOB)", usd(cost.insurance_usd), muted=True)
    row("Valor CIF", usd(cost.cif_usd), muted=True)
    charges = [(f"Derechos de aduana ({fmt_num(rules.duty_pct, 0, 2)} % del CIF)", cost.duty_usd),
               (f"IVA ({vat} % de CIF + derechos)", cost.vat_usd),
               ("Honorario de desaduanamiento", cost.brokerage_usd),
               (f"IVA del honorario ({vat} %)", cost.brokerage_vat_usd)]
    in_pesos = cost.import_charges_clp
    for (label, value), pesos in zip(charges, in_pesos):
        row(label, usd(value), clp(pesos))
    row("Subtotal importación", usd(cost.import_total_usd), clp(sum(in_pesos)), bold=True)
    section(f"Total puesto en Chile · {fmt_int(boards)} {'placa' if boards == 1 else 'placas'}")
    total_usd = usd(cost.total) if not clp_account else ""
    row("Total con todo incluido", total_usd, clp(cost.total_clp), bold=True)
    if boards:
        per_board = cost.total / boards
        row("Por placa", usd(per_board) if not clp_account else "",
            fmt_num(per_board, 0) if clp_account else fmt_num(cost.total_clp / boards, 0), bold=True)
    row("IVA incluido (crédito fiscal)", fmt_money(cost.vat_total) if not clp_account else "",
        clp(cost.vat_total) if clp_account else "", muted=True)
    row("Costo sin IVA", fmt_money(cost.net_total) if not clp_account else "",
        clp(cost.net_total) if clp_account else "")
    notes = [rates_text(cost.setup), rules_text(cost.setup)] + list(rules.notes)
    footer = "".join(f"<p style='color:#59636E; margin:4px 0'>{escape(note)}</p>" for note in notes)
    return ("<table width='100%' cellspacing='0' cellpadding='3'>"
            "<tr style='color:#59636E'><td></td><td align='right'>USD</td><td align='right'>CLP</td></tr>"
            + "".join(rows) + "</table>" + footer)


class LandedDialog(QDialog):
    def __init__(self, cost: LandedCost, boards: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Precio con todo incluido: desglose")
        self.resize(600, 680)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setHtml(landed_html(cost, boards))
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox()
        buttons.addButton("Cerrar", QDialogButtonBox.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
