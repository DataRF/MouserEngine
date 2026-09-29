"""Exportación de la cotización a Excel y del carro de compra a CSV."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .models import (
    LEVEL_ERROR,
    LEVEL_EXCLUDED,
    LEVEL_OK,
    LEVEL_WARN,
    BomItem,
    ItemQuote,
    QuoteParams,
    QuoteSummary,
    lead_time_label,
    packaging_label,
)
from .cart import cart_lines
from .formatting import fmt_num
from .landed import month_name
from .pricing import format_breaks
from .scenarios import CostModel, CurvePoint, ScenarioRow, scenario_table

_FILLS = {
    LEVEL_OK: "E3F4E8",
    LEVEL_WARN: "FFF4CC",
    LEVEL_ERROR: "FBE0E0",
    LEVEL_EXCLUDED: "EEEEEE",
}
_HEADER_FILL = "1F3A5F"
_PRICE_FMT = "#,##0.00###"
_MONEY_FMT = "#,##0.00"
_INT_FMT = "#,##0"
_PCT_FMT = '+0.0%;-0.0%;0.0%'
_SERIES_COLORS = ("2A78D6", "EB6834")  # mismos colores que el gráfico de la aplicación
BIG_DROP = -10  # % de baja del costo por placa que se destaca

DETAIL_COLUMNS = [
    ("Ítem", 6), ("Líneas BOM", 10), ("Comprar", 9), ("Designadores", 22), ("MPN (BOM)", 22),
    ("Fabricante (BOM)", 18), ("Descripción (BOM)", 30), ("Cant./placa", 10), ("Cant. requerida", 12),
    ("N° Mouser", 22), ("MPN Mouser", 22), ("Fabricante Mouser", 18), ("Descripción Mouser", 36),
    ("Empaque", 14), ("Mín.", 8), ("Múlt.", 8), ("Cant. a comprar", 12), ("Precio unit.", 12),
    ("Total línea", 13), ("Moneda", 8), ("Stock Mouser", 12), ("Plazo fábrica", 13),
    ("En pedido a fábrica", 22), ("Ciclo de vida", 18), ("RoHS", 14), ("Cant. óptima", 12),
    ("Total óptimo", 13), ("Ahorro posible", 13), ("Tramos de precio", 45), ("Estado", 20),
    ("Observaciones", 70), ("Link Mouser", 16), ("Datasheet", 14), ("Consultado", 17),
]


def _page_setup(sheet, landscape: bool, fit_width: bool = True) -> None:
    """Configuración de impresión.

    Hojas angostas: ajustadas al ancho de una página. Hojas anchas (detalle): horizontal al 70 %,
    repitiendo la fila de encabezado y las primeras columnas en cada página para que se lean.
    """
    sheet.page_setup.orientation = "landscape" if landscape else "portrait"
    if fit_width:
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
    else:
        sheet.page_setup.scale = 70
        sheet.print_title_cols = "A:B"
    if landscape:
        sheet.print_title_rows = "1:1"


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _detail_row(item: BomItem, q: ItemQuote) -> list:
    part = q.part
    on_order = ", ".join(f"{o.quantity} ({o.date})" if o.date else str(o.quantity)
                         for o in (part.on_order if part else []))
    return [
        item.id,
        item.rows_label,
        "Sí" if item.include else "No",
        item.designators,
        item.mpn,
        item.manufacturer,
        item.display_description,
        item.qty_per_board,
        q.required,
        part.mouser_pn if part else "",
        part.mpn if part else "",
        part.manufacturer if part else "",
        part.description if part else "",
        packaging_label(part.packaging) if part else "",
        part.min_qty if part else None,
        part.mult if part else None,
        q.buy_qty or None,
        _num(q.unit_price),
        _num(q.ext_price),
        q.currency,
        part.stock if part else None,
        lead_time_label(part.lead_time) if part else "",
        on_order,
        part.lifecycle_label if part else "",
        part.rohs if part else "",
        q.opt_qty or None,
        _num(q.opt_ext_price),
        _num(q.savings) if q.savings else None,
        format_breaks(part.price_breaks) if part else "",
        q.status,
        " ".join(q.notes),
        part.product_url if part else "",
        part.datasheet_url if part else "",
        item.looked_up_at.strftime("%d-%m-%Y %H:%M") if item.looked_up_at else "",
    ]


def export_excel(
    path: str | Path,
    items: list[BomItem],
    quotes: list[ItemQuote],
    summary: QuoteSummary,
    params: QuoteParams,
    bom_path: str = "",
    bom_grid: list[list[str]] | None = None,
    queried_at: datetime | None = None,
    scenario_quantities: list[int] | None = None,
    scenario_max: int | None = None,
    client_name: str = "",
    company_name: str = "",
) -> Path:
    """Escribe la cotización en Excel.

    Con `scenario_quantities` agrega la hoja «Escenarios» (costo por placa y total para cada
    cantidad, con dos gráficos nativos de Excel hasta `scenario_max` placas).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    path = Path(path)
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
    thin = Side(style="thin", color="D0D7DE")
    border = Border(bottom=thin)
    currency = summary.currency or ""

    # --- Resumen ------------------------------------------------------------
    ws = wb.active
    ws.title = "Resumen"
    ws["A1"] = "Cotización Mouser"
    ws["A1"].font = Font(bold=True, size=16, color=_HEADER_FILL)
    now = datetime.now()
    rows: list[tuple[str, object, str | None]] = []
    if company_name:
        rows.append(("Empresa", company_name, None))
    if client_name:
        rows.append(("Cliente", client_name, None))
    rows += [
        ("Generado", now.strftime("%d-%m-%Y %H:%M"), None),
        ("Precios y stock consultados", queried_at.strftime("%d-%m-%Y %H:%M") if queried_at else "—", None),
        ("Archivo BOM", Path(bom_path).name if bom_path else "—", None),
        ("Placas / equipos", params.boards, _INT_FMT),
        ("Merma general (%)", params.spares_pct, "0.0"),
        ("Merma pasivos (%)", params.passive_spares_pct, "0.0"),
        ("Optimización por tramos", "Sí" if params.optimize_breaks else "No", None),
        ("Moneda", currency or "—", None),
        (None, None, None),
        ("Partes (líneas consolidadas)", summary.items, _INT_FMT),
        ("Incluidas en la compra", summary.included, _INT_FMT),
        ("OK", summary.ok, _INT_FMT),
        ("Con advertencias", summary.warn, _INT_FMT),
        ("Con problemas", summary.error, _INT_FMT),
        ("Sin precio (no suman al total)", summary.unpriced, _INT_FMT),
        (None, None, None),
        ("Subtotal compra mínima", _num(summary.subtotal), _MONEY_FMT),
        ("Subtotal optimizado por tramos", _num(summary.optimized_subtotal), _MONEY_FMT),
        ("Ahorro posible por tramos", _num(summary.savings), _MONEY_FMT),
        ("Subtotal componentes (usado)", _num(summary.goods), _MONEY_FMT),
    ]
    rows += _cost_rows(summary, params)
    row_index = 3
    for label, value, fmt in rows:
        if label is not None:
            ws.cell(row=row_index, column=1, value=label).font = Font(bold=label.startswith("Total"))
            cell = ws.cell(row=row_index, column=2, value=value)
            if fmt:
                cell.number_format = fmt
            if label.startswith("Total"):
                cell.font = Font(bold=True)
        row_index += 1
    row_index += 1
    notes = ["Precios y stock según la Mouser Search API al momento de la consulta; pueden cambiar."]
    if summary.landed is not None:
        notes.append("El total puesto en Chile suma el flete de Mouser y lo que cobra DHL al importar: derechos, "
                     "IVA y honorario de desaduanamiento con su IVA, al dólar aduanero del mes. Es una estimación: "
                     "el flete y el honorario pueden variar con el peso y el valor del envío.")
    elif not (summary.freight or summary.duty or summary.vat):
        notes.append("El total no incluye flete, derechos de aduana, IVA ni desaduanamiento "
                     "(active «Precio con todo incluido» en la aplicación).")
    if summary.mixed_currency:
        notes.append("ATENCIÓN: hay partes cotizadas en monedas distintas.")
    for note in notes:
        cell = ws.cell(row=row_index, column=1, value=note)
        cell.font = Font(italic=True, color="555555")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)
        ws.row_dimensions[row_index].height = 30
        row_index += 1
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 22
    _page_setup(ws, landscape=False)

    # --- Escenarios de volumen -------------------------------------------------
    if scenario_quantities:
        quantities = sorted(set(q for q in scenario_quantities if q > 0))
        maximum = max(scenario_max or 0, quantities[-1] if quantities else 1, 10)
        scenario_rows = scenario_table(items, params, quantities)
        curve = CostModel(items, params).curve(maximum, extra=quantities)
        _write_scenarios(wb.create_sheet("Escenarios"), scenario_rows, curve, params, currency, maximum,
                         client_name, header_font, header_fill, border)

    # --- Detalle y Problemas ---------------------------------------------------
    def write_detail(sheet, pairs):
        for col, (title, width) in enumerate(DETAIL_COLUMNS, start=1):
            cell = sheet.cell(row=1, column=col, value=title)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.column_dimensions[get_column_letter(col)].width = width
        sheet.row_dimensions[1].height = 30
        titles = [t for t, _ in DETAIL_COLUMNS]
        formats = {
            "Precio unit.": _PRICE_FMT, "Total línea": _MONEY_FMT, "Total óptimo": _MONEY_FMT,
            "Ahorro posible": _MONEY_FMT, "Cant./placa": _INT_FMT, "Cant. requerida": _INT_FMT,
            "Mín.": _INT_FMT, "Múlt.": _INT_FMT, "Cant. a comprar": _INT_FMT, "Stock Mouser": _INT_FMT,
            "Cant. óptima": _INT_FMT,
        }
        centered = {"Ítem", "Líneas BOM", "Comprar", "Moneda"}
        status_col = titles.index("Estado") + 1
        link_col = titles.index("Link Mouser") + 1
        sheet_col = titles.index("Datasheet") + 1
        for r, (item, q) in enumerate(pairs, start=2):
            values = _detail_row(item, q)
            for c, value in enumerate(values, start=1):
                cell = sheet.cell(row=r, column=c, value=value)
                fmt = formats.get(titles[c - 1])
                if fmt:
                    cell.number_format = fmt
                if titles[c - 1] in centered:
                    cell.alignment = Alignment(horizontal="center")
                cell.border = border
            fill = _FILLS.get(q.level)
            if fill:
                sheet.cell(row=r, column=status_col).fill = PatternFill("solid", fgColor=fill)
            for col, label in ((link_col, "Ver en Mouser"), (sheet_col, "Datasheet")):
                cell = sheet.cell(row=r, column=col)
                if cell.value:
                    cell.hyperlink = str(cell.value)
                    cell.value = label
                    cell.font = Font(color="0563C1", underline="single")
        sheet.freeze_panes = "B2"
        last = get_column_letter(len(DETAIL_COLUMNS))
        sheet.auto_filter.ref = f"A1:{last}{max(1, len(pairs) + 1)}"
        _page_setup(sheet, landscape=True, fit_width=False)

    pairs = list(zip(items, quotes))
    write_detail(wb.create_sheet("Detalle"), pairs)
    problems = [(i, q) for i, q in pairs if q.level in (LEVEL_WARN, LEVEL_ERROR)]
    write_detail(wb.create_sheet("Problemas"), problems)

    # --- Carro Mouser ----------------------------------------------------------
    cart = wb.create_sheet("Carro Mouser")
    for col, (title, width) in enumerate(
            [("Mouser Part Number", 24), ("Quantity", 10), ("Customer Part Number", 30),
             ("Manufacturer Part Number", 26), ("Description", 50)], start=1):
        cell = cart.cell(row=1, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cart.column_dimensions[get_column_letter(col)].width = width
    for r, row in enumerate(cart_rows(items, quotes), start=2):
        for c, value in enumerate(row, start=1):
            cart.cell(row=r, column=c, value=value)
    _page_setup(cart, landscape=True)

    # --- BOM original ----------------------------------------------------------
    if bom_grid:
        raw = wb.create_sheet("BOM original")
        for r, row in enumerate(bom_grid, start=1):
            for c, value in enumerate(row, start=1):
                if value != "":
                    raw.cell(row=r, column=c, value=value)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _pct(value) -> str:
    return fmt_num(value, 0, 2)


def _cost_rows(summary: QuoteSummary, params: QuoteParams) -> list[tuple[str | None, object, str | None]]:
    """Filas de costos del Resumen: el desglose del precio puesto en Chile o el total de Mouser."""
    cost = summary.landed
    if cost is None:
        rows: list[tuple[str | None, object, str | None]] = []
        if summary.freight:  # cotizaciones antiguas con costos ingresados a mano
            rows.append(("Flete", _num(summary.freight), _MONEY_FMT))
        if summary.duty:
            rows.append((f"Arancel ({params.duty_pct:g}%)", _num(summary.duty), _MONEY_FMT))
        if summary.vat:
            rows.append((f"IVA ({params.vat_pct:g}%)", _num(summary.vat), _MONEY_FMT))
        rows.append(("Total estimado", _num(summary.total), _MONEY_FMT))
        if summary.total_clp is not None:
            rate = params.fx_rate or (params.import_setup.usd_rate if params.import_setup is not None else None)
            if rate:
                rows.append(("Tipo de cambio (CLP)", float(rate), "#,##0.00"))
            rows.append(("Total estimado en CLP", _num(summary.total_clp), "#,##0"))
        return rows
    rules, rates = cost.setup.rules, cost.setup.rates
    vat = _pct(rules.vat_pct)
    rows = [
        ("Flete de Mouser", _num(cost.freight), _MONEY_FMT),
        ("Subtotal Mouser (mercancía y flete)", _num(cost.mouser_total), _MONEY_FMT),
        (f"Derechos de aduana ({_pct(rules.duty_pct)} % del CIF)", _num(cost.duty), _MONEY_FMT),
        (f"IVA de importación ({vat} %)", _num(cost.vat), _MONEY_FMT),
        ("Honorario de desaduanamiento DHL", _num(cost.brokerage), _MONEY_FMT),
        (f"IVA del honorario ({vat} %)", _num(cost.brokerage_vat), _MONEY_FMT),
        ("Subtotal importación (DHL)", _num(cost.import_total), _MONEY_FMT),
        ("Total puesto en Chile (todo incluido)", _num(cost.total), _MONEY_FMT),
    ]
    if summary.total_clp is not None:
        rows.append(("Total puesto en Chile en CLP", _num(summary.total_clp), "#,##0"))
    rows += [
        ("IVA incluido (crédito fiscal)", _num(cost.vat_total), _MONEY_FMT),
        ("Costo sin IVA", _num(cost.net_total), _MONEY_FMT),
        (None, None, None),
        ("Valor FOB (USD)", _num(cost.fob_usd), _MONEY_FMT),
        ("Valor CIF (USD)", _num(cost.cif_usd), _MONEY_FMT),
        ("Dólar observado (CLP)" + (f" del {_day(rates.usd_date)}" if rates.usd_date else ""),
         float(cost.usd_rate), "#,##0.00"),
        ("Dólar aduanero (CLP)" + (f" de {month_name(rates.customs_month)}" if rates.customs_month else ""),
         float(cost.customs_rate), "#,##0.00"),
    ]
    if cost.setup.estimated_rates:
        rows.append(("Tipo de cambio", "de referencia (sin conexión)", None))
    return rows


def _day(text: str) -> str:
    """"2026-09-29" -> "29-09-2026"."""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return text


def _chart_title(text: str):
    """Título de gráfico en 11 pt (el predeterminado de Excel es demasiado grande para estos gráficos)."""
    from openpyxl.chart.text import RichText, Text
    from openpyxl.chart.title import Title
    from openpyxl.drawing.text import CharacterProperties, Paragraph, ParagraphProperties, RegularTextRun

    props = CharacterProperties(sz=1100, b=True)
    paragraph = Paragraph(pPr=ParagraphProperties(defRPr=props), r=[RegularTextRun(rPr=props, t=text)])
    return Title(tx=Text(rich=RichText(p=[paragraph])), overlay=False)


def _write_scenarios(sheet, rows: list[ScenarioRow], curve: list[CurvePoint], params: QuoteParams,
                     currency: str, maximum: int, client_name: str, header_font, header_fill, border) -> None:
    from openpyxl.chart import Reference, ScatterChart, Series
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    sheet["A1"] = "Escenarios de volumen"
    sheet["A1"].font = Font(bold=True, size=16, color=_HEADER_FILL)
    details = []
    if client_name:
        details.append(f"Cliente: {client_name}")
    details.append(f"Merma general {params.spares_pct:g} %, pasivos {params.passive_spares_pct:g} %")
    details.append("optimización por tramos: " + ("sí" if params.optimize_breaks else "no"))
    if params.landed and params.import_setup is not None:
        details.append("precio puesto en Chile: incluye flete, aduana, IVA y desaduanamiento")
    elif params.freight or params.duty_pct or params.vat_pct:
        details.append("incluye flete, arancel e IVA ingresados")
    details.append("supone stock disponible de todas las partes")
    sheet["A2"] = " · ".join(details)
    sheet["A2"].font = Font(italic=True, color="555555")

    money = f" ({currency})" if currency else ""
    columns = [("Placas", 11, _INT_FMT), (f"Costo por placa{money}", 16, _PRICE_FMT),
               ("Variación por placa", 14, _PCT_FMT), (f"Costo total{money}", 16, _MONEY_FMT),
               (f"Solo componentes{money}", 16, _MONEY_FMT), (f"Ahorro posible por tramos{money}", 17, _MONEY_FMT),
               ("Partes sin stock suficiente", 15, _INT_FMT), ("Partes sin precio", 12, _INT_FMT)]
    header_row = 4
    for col, (title, width, _) in enumerate(columns, start=1):
        cell = sheet.cell(row=header_row, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.row_dimensions[header_row].height = 46
    for r, row in enumerate(rows, start=header_row + 1):
        values = [row.boards, _num(row.total_unit), float(row.change) / 100 if row.change is not None else None,
                  _num(row.total), _num(row.goods), _num(row.savings) if row.savings else None,
                  row.short or None, row.unpriced or None]
        for c, value in enumerate(values, start=1):
            cell = sheet.cell(row=r, column=c, value=value)
            cell.number_format = columns[c - 1][2]
            cell.border = border
        if row.total_unit is not None:  # como en la aplicación: 2 decimales, 4 si es menos de 1
            sheet.cell(row=r, column=2).number_format = _MONEY_FMT if row.total_unit >= 1 else "#,##0.00##"
        if row.change is not None and row.change <= BIG_DROP:
            sheet.cell(row=r, column=3).font = Font(bold=True, color="1A7F37")
    last_row = header_row + len(rows)
    note_row = last_row + 1
    sheet.cell(row=note_row, column=1,
               value="En verde, bajas de 10 % o más del costo por placa respecto de la cantidad anterior. "
                     "Las partes sin precio no se suman.").font = Font(italic=True, color="555555")

    # Datos de la curva (a la derecha de la tabla), para los gráficos.
    data_col = len(columns) + 2
    for offset, (title, fmt) in enumerate([("Placas", _INT_FMT), (f"Costo por placa{money}", _PRICE_FMT),
                                           (f"Costo total{money}", _MONEY_FMT)]):
        cell = sheet.cell(row=header_row, column=data_col + offset, value=title)
        cell.font = Font(bold=True, color="555555")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(data_col + offset)].width = 14
    sheet.cell(row=header_row - 1, column=data_col, value="Datos de los gráficos").font = Font(italic=True,
                                                                                               color="555555")
    points = [p for p in curve if p.priced]
    for r, point in enumerate(points, start=header_row + 1):
        sheet.cell(row=r, column=data_col, value=point.boards).number_format = _INT_FMT
        sheet.cell(row=r, column=data_col + 1, value=round(point.total_unit, 4)).number_format = _PRICE_FMT
        sheet.cell(row=r, column=data_col + 2, value=round(point.total, 2)).number_format = _MONEY_FMT
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    if points:
        first, last = header_row + 1, header_row + len(points)
        xs = Reference(sheet, min_col=data_col, min_row=first, max_row=last)
        anchor_row = note_row + 2

        def width_cm(first_col: int, last_col: int) -> float:  # ancho de columnas de Excel en cm (96 ppp)
            pixels = sum(columns[c - 1][1] * 7 + 5 for c in range(first_col, last_col + 1))
            return pixels / 37.8 - 0.2

        specs = [(data_col + 1, "Costo por placa según la cantidad", f"{currency} por placa".strip(),
                  _SERIES_COLORS[0], "A", width_cm(1, 4)),
                 (data_col + 2, "Costo total según la cantidad", f"Total {currency}".strip(), _SERIES_COLORS[1],
                  "E", width_cm(5, 8))]
        unit_top = max(p.total_unit for p in points)
        for col, title, y_title, color, anchor_col, width in specs:
            chart = ScatterChart()
            chart.title = _chart_title(title)
            chart.style = 2
            chart.height = 7.5
            chart.width = width
            chart.legend = None
            chart.x_axis.title = "Placas (escala logarítmica)"
            chart.y_axis.title = y_title
            chart.x_axis.scaling.logBase = 10
            chart.x_axis.scaling.min = 1
            chart.x_axis.scaling.max = maximum
            chart.y_axis.scaling.min = 0
            chart.x_axis.number_format = _INT_FMT
            chart.y_axis.number_format = "#,##0.00" if col == data_col + 1 and unit_top < 10 else _INT_FMT
            chart.x_axis.majorGridlines = None
            chart.x_axis.delete = False  # Excel reciente oculta los ejes si no se indica
            chart.y_axis.delete = False
            series = Series(Reference(sheet, min_col=col, min_row=first, max_row=last), xs, title=title)
            series.marker.symbol = "none"
            series.smooth = False
            series.graphicalProperties.line.solidFill = color
            series.graphicalProperties.line.width = 28575  # 2,25 pt
            chart.series.append(series)
            sheet.add_chart(chart, f"{anchor_col}{anchor_row}")
        sheet.print_area = f"A1:{get_column_letter(len(columns))}{anchor_row + 15}"
    _page_setup(sheet, landscape=True)


def cart_rows(items: list[BomItem], quotes: list[ItemQuote]) -> list[list]:
    """Filas para cargar el carro en Mouser: código Mouser, cantidad y referencia (≤ 21 caracteres)."""
    return [[line.mouser_pn, line.quantity, line.customer_pn, line.mpn, line.description]
            for line in cart_lines(items, quotes)]


def export_cart_csv(path: str | Path, items: list[BomItem], quotes: list[ItemQuote]) -> int:
    path = Path(path)
    rows = cart_rows(items, quotes)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Mouser Part Number", "Quantity", "Customer Part Number",
                         "Manufacturer Part Number", "Description"])
        writer.writerows(rows)
    return len(rows)
