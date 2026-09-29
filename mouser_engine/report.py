"""Informe de costos para el cliente: los datos del análisis por volumen (sin Qt).

El informe es genérico (no usa la plantilla de la empresa) y está pensado para la empresa para la
que se diseña: es un análisis comercial que muestra cuánto cuestan los componentes según la
cantidad a fabricar (puestos en Chile si se pidió el precio con todo incluido), qué partes pesan
más y los riesgos de stock. No incluye el BOM ni margen de venta.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from .formatting import fmt_int, fmt_money, fmt_num
from .landed import month_name
from .models import (
    LEVEL_ERROR,
    LEVEL_EXCLUDED,
    LEVEL_WARN,
    BomItem,
    ItemQuote,
    QuoteParams,
    QuoteSummary,
    lead_time_label,
)
from .quote import quote_all, summarize
from .scenarios import CostModel, CurvePoint, ScenarioRow, scenario_table

TOP_PARTS = 8

# Estados que el informe muestra tal cual y su gravedad; el resto se resume como "Revisar".
_LIFECYCLE_STATUSES = {"Obsoleto", "Descontinuado", "Fin de vida (EOL)", "Última compra (LTB)",
                       "No recomendado (NRND)"}


@dataclass
class ReportLine:
    """Una parte del BOM a la cantidad de referencia, con su precio en cada cantidad comparada."""

    number: int
    designators: str
    mpn: str
    manufacturer: str
    description: str
    qty_per_board: int
    required: int
    buy_qty: int
    unit_price: Decimal | None
    ext_price: Decimal | None
    share: Decimal | None  # % del costo de componentes
    stock: int | None
    level: str
    status: str
    by_spec: bool
    short_from: int | None = None  # primera cantidad comparada en que el stock de Mouser no alcanza


@dataclass
class Observation:
    level: str  # LEVEL_WARN o LEVEL_ERROR (color del título)
    title: str
    items: list[str]


@dataclass
class ClientReport:
    client: str
    project: str
    generated_at: datetime
    queried_at: datetime | None
    currency: str
    params: QuoteParams
    summary: QuoteSummary
    quantities: list[int]
    scenarios: list[ScenarioRow]
    curve: list[CurvePoint]
    max_boards: int
    lines: list[ReportLine]
    top: list[ReportLine]
    observations: list[Observation]
    notes: list[str]
    chart_mode: str = "overlay"

    @property
    def boards(self) -> int:
        return self.params.boards

    @property
    def cost_per_board(self) -> Decimal | None:
        return (self.summary.total / self.boards).quantize(Decimal("0.0001")) if self.boards else None

    @property
    def priced_lines(self) -> list[ReportLine]:
        return [line for line in self.lines if line.ext_price is not None]

    def scenario(self, boards: int) -> ScenarioRow | None:
        return next((row for row in self.scenarios if row.boards == boards), None)

    @property
    def cheapest(self) -> ScenarioRow | None:
        """El escenario de mayor cantidad con precio (normalmente el menor costo por placa)."""
        rows = [row for row in self.scenarios if row.total_unit is not None and row.priced]
        return rows[-1] if rows else None


def _part_label(item: BomItem) -> str:
    if item.mpn or item.mouser_pn:
        return item.mpn or item.mouser_pn
    if item.spec is not None:
        return item.spec.label()
    return item.value or item.description or f"Línea {item.rows_label}"


def _describe(item: BomItem, q: ItemQuote) -> str:
    return item.display_description or (q.part.description if q.part else "")


def build_client_report(items: list[BomItem], params: QuoteParams, quantities: list[int], max_boards: int,
                        client: str = "", project: str = "", queried_at: datetime | None = None,
                        chart_mode: str = "overlay", generated_at: datetime | None = None) -> ClientReport:
    """Arma el informe para `params.boards` placas (cantidad de referencia) y las cantidades comparadas."""
    quantities = sorted({int(q) for q in quantities if int(q) > 0})
    boards = max(1, int(params.boards))
    params = replace(params, boards=boards, assume_stock=True)  # análisis de volumen: se supone stock
    max_boards = max(int(max_boards), quantities[-1] if quantities else 1, boards, 10)
    quotes = quote_all(items, params)
    summary = summarize(items, quotes, params)
    by_quantity = {qty: quote_all(items, replace(params, boards=qty)) for qty in quantities}
    scenarios = scenario_table(items, params, quantities)
    curve = CostModel(items, params).curve(max_boards, extra=quantities + [boards])

    lines: list[ReportLine] = []
    for index, (item, q) in enumerate(zip(items, quotes)):
        if q.level == LEVEL_EXCLUDED:
            continue
        part = q.part
        share = None
        if q.ext_price is not None and summary.goods:
            share = (q.ext_price / summary.goods * 100).quantize(Decimal("0.1"))
        short_from = None
        for qty in quantities:
            other = by_quantity[qty][index]
            if (short_from is None and other.part is not None and other.part.stock is not None and other.buy_qty
                    and other.part.stock < other.buy_qty):
                short_from = qty
        lines.append(ReportLine(
            number=item.id, designators=item.designators, mpn=part.mpn if part else _part_label(item),
            manufacturer=part.manufacturer if part else item.manufacturer, description=_describe(item, q),
            qty_per_board=item.qty_per_board, required=q.required, buy_qty=q.buy_qty, unit_price=q.unit_price,
            ext_price=q.ext_price, share=share, stock=part.stock if part else None, level=q.level, status=q.status,
            by_spec=item.by_spec and item.manual_part is None, short_from=short_from))

    top = sorted((line for line in lines if line.ext_price), key=lambda line: -line.ext_price)[:TOP_PARTS]
    report = ClientReport(
        client=client.strip(), project=project.strip(), generated_at=generated_at or datetime.now(),
        queried_at=queried_at, currency=summary.currency, params=params, summary=summary, quantities=quantities,
        scenarios=scenarios, curve=curve, max_boards=max_boards, lines=lines, top=top, observations=[], notes=[],
        chart_mode=chart_mode if chart_mode in ("overlay", "split") else "overlay")
    report.observations = _observations(items, quotes, lines, report)
    report.notes = _notes(report, items)
    return report


def _who(line: ReportLine) -> str:
    return f"{line.mpn} ({line.designators})" if line.designators else line.mpn


def _observations(items: list[BomItem], quotes: list[ItemQuote], lines: list[ReportLine],
                  report: ClientReport) -> list[Observation]:
    boards = report.boards
    by_number = {line.number: line for line in lines}
    stock_now, stock_later, lifecycle, unpriced, moq, review = [], [], [], [], [], []
    for item, q in zip(items, quotes):
        line = by_number.get(item.id)
        if line is None:
            continue
        part = q.part
        if line.ext_price is None:
            reason = {"No encontrado": "no se encontró en Mouser", "Sin precio": "Mouser no informa precio",
                      "Sin N° de parte": "el BOM no indica número de parte",
                      "Sin opción que cumpla": "ninguna opción de Mouser cumple la especificación",
                      "Especificación incompleta": "falta información en el BOM para elegirla",
                      "Error de consulta": "no se pudo consultar"}.get(q.status, q.status.lower() or "sin precio")
            unpriced.append(f"{_who(line)}: {reason}.")
            continue
        if part is not None and part.stock is not None and q.buy_qty and part.stock < q.buy_qty:
            detail = ("sin stock en Mouser" if part.stock <= 0
                      else f"hay {fmt_int(part.stock)} en stock y se necesitan {fmt_int(q.buy_qty)}")
            if part.lead_time:
                detail += f"; plazo de fábrica {lead_time_label(part.lead_time)}"
            if part.on_order:
                entries = ", ".join(f"{fmt_int(o.quantity)} para el {_date(o.date)}" if o.date else fmt_int(o.quantity)
                                    for o in part.on_order[:2])
                detail += f"; en pedido a fábrica: {entries}"
            stock_now.append(f"{_who(line)}: {detail}.")
        elif line.short_from is not None and line.short_from > boards and part is not None and part.stock is not None:
            stock_later.append(f"{_who(line)}: {fmt_int(part.stock)} en stock; no alcanza desde "
                               f"{fmt_int(line.short_from)} placas.")
        if part is not None and part.lifecycle_label in _LIFECYCLE_STATUSES:
            text = f"{_who(line)}: {part.lifecycle_label.lower()} según Mouser"
            if part.suggested_replacement:
                text += f"; reemplazo sugerido: {part.suggested_replacement}"
            lifecycle.append(text + ".")
        if part is not None and q.buy_qty and q.required and part.min_qty > 1 and q.buy_qty >= 5 * q.required:
            moq.append(f"{_who(line)}: se deben comprar {fmt_int(q.buy_qty)} para usar {fmt_int(q.required)} "
                       f"(venta mínima de {fmt_int(part.min_qty)}).")
        if q.status in ("Coincidencia aproximada", "Fabricante distinto", "Varios fabricantes", "Revisar tensión",
                        "Fabricante no reconocido"):
            review.append(f"{_who(line)}: {q.status.lower()}.")

    result = []
    if stock_now:
        result.append(Observation(LEVEL_ERROR, f"Stock insuficiente para {fmt_int(boards)} placas", stock_now))
    if stock_later:
        result.append(Observation(LEVEL_WARN, "Stock que no alcanza para volúmenes mayores", stock_later))
    if lifecycle:
        result.append(Observation(LEVEL_WARN, "Ciclo de vida", lifecycle))
    if unpriced:
        result.append(Observation(LEVEL_ERROR, "Partes sin precio (no incluidas en los costos)", unpriced))
    if moq:
        result.append(Observation(LEVEL_WARN, "Compra mínima mayor a lo necesario", moq))
    if review:
        result.append(Observation(LEVEL_WARN, "Partes a confirmar", review))
    return result


def _date(text: str) -> str:
    """"2026-11-10" -> "10-11-2026"."""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return text


def _notes(report: ClientReport, items: list[BomItem]) -> list[str]:
    p = report.params
    when = report.queried_at.strftime("%d-%m-%Y %H:%M") if report.queried_at else "la fecha del informe"
    notes = [f"Precios y disponibilidad según Mouser Electronics al {when}. Pueden cambiar sin aviso: "
             "se confirman al momento de la compra."]
    spares = f"una merma de {p.spares_pct:g} %" if p.spares_pct else "sin merma"
    if p.passive_spares_pct:
        spares += f" ({p.passive_spares_pct:g} % en resistencias, condensadores e inductores)"
    notes.append("Las cantidades de compra respetan el mínimo de venta y el múltiplo de cada parte, "
                 f"con {spares}.")
    cost = report.summary.landed
    extras = []
    if p.freight:
        extras.append(f"flete estimado de {fmt_money(p.freight, report.currency)}")
    if p.duty_pct:
        extras.append(f"arancel de {p.duty_pct:g} %")
    if p.vat_pct:
        extras.append(f"IVA de {p.vat_pct:g} %")
    if cost is not None:
        rules, rates = cost.setup.rules, cost.setup.rates
        notes.append("Los costos son puestos en Chile (todo incluido): suman el flete de Mouser y lo que se paga al "
                     f"importar por DHL Express: derechos de aduana ({fmt_num(rules.duty_pct, 0, 2)} % del valor CIF), "
                     f"IVA ({fmt_num(rules.vat_pct, 0, 2)} %) y el honorario de desaduanamiento con su IVA. "
                     "El flete y el honorario son valores de referencia y pueden variar con el peso y el valor "
                     "del envío.")
        if cost.setup.estimated_rates:
            notes.append(f"Tipo de cambio de referencia: {fmt_num(cost.usd_rate, 2)} CLP por USD (no se pudo obtener "
                         "el del día).")
        else:
            usd = f"dólar observado de {fmt_num(cost.usd_rate, 2)} CLP"
            if rates.usd_date:
                usd += f" del {_date(rates.usd_date)}"
            customs = f"dólar aduanero de {fmt_num(cost.customs_rate, 2)} CLP"
            if rates.customs_month:
                customs += f" ({month_name(rates.customs_month)})"
            notes.append(f"Tipo de cambio: {usd}; lo que cobra DHL se convierte al {customs}, según el Banco "
                         "Central de Chile.")
        notes.append("El IVA incluido es crédito fiscal para una empresa contribuyente de IVA.")
    elif extras:
        notes.append("Los costos incluyen " + ", ".join(extras) + " (valores de referencia).")
    else:
        notes.append("Los costos son los precios de Mouser: no incluyen flete, derechos de aduana, IVA ni "
                     "desaduanamiento.")
    notes.append("El análisis por volumen supone que habrá stock de todas las partes: en cada cantidad se considera "
                 "la opción de menor costo. Las observaciones indican dónde el stock actual de Mouser no alcanza.")
    if p.optimize_breaks:
        notes.append("Se aplicó optimización por tramos de precio: en algunas partes se compran más unidades "
                     "porque el total resulta menor.")
    if any(line.by_spec for line in report.lines):
        notes.append("Las resistencias y condensadores sin número de parte en el BOM se cotizaron con una opción "
                     "que cumple o supera la especificación indicada (valor, encapsulado, tolerancia, potencia, "
                     "tensión y dieléctrico), de un fabricante reconocido.")
    if p.fx_rate and report.summary.total_clp is not None:
        notes.append(f"Tipo de cambio de referencia: {p.fx_rate:g} CLP por {report.currency}.")
    notes.append("El análisis considera solo los componentes: no incluye la fabricación del circuito impreso, el "
                 "ensamblaje ni otros costos del producto.")
    return notes
