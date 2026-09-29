"""Escenarios de volumen: costo total y costo por placa según la cantidad a fabricar.

- `scenario_table` usa el motor de cotización exacto (Decimal, mismas reglas que la tabla de
  partes) para las cantidades elegidas (1, 10, 25, 50, 100, 500, 1.000…).
- `CostModel` calcula lo mismo con números de punto flotante y datos precalculados, para
  dibujar la curva completa al instante mientras se mueve la barra de cantidad. Las pruebas
  verifican que ambos coincidan.

El análisis supone que hay stock de todas las partes: en cada cantidad se elige la opción más
conveniente por precio aunque hoy Mouser no tenga stock suficiente (igual se cuentan esas partes).
Con el precio con todo incluido, el costo suma el flete, la aduana, el IVA y el desaduanamiento.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction

from .landed import landed_cost
from .models import LEVEL_EXCLUDED, LEVEL_ORDER, BomItem, Part, QuoteParams
from .passives import candidate_spec, is_recognized
from .quote import quote_all, summarize
from .utils import mfr_match


@dataclass
class ScenarioRow:
    boards: int
    currency: str
    goods: Decimal
    unit: Decimal | None
    total: Decimal
    total_unit: Decimal | None
    savings: Decimal
    included: int
    priced: int
    unpriced: int
    short: int  # partes cuyo stock actual en Mouser no alcanza para esa cantidad
    change: Decimal | None = None  # variación del costo total por placa vs la fila anterior (%)


def scenario_table(items: list[BomItem], params: QuoteParams, quantities: list[int]) -> list[ScenarioRow]:
    rows: list[ScenarioRow] = []
    previous: Decimal | None = None
    for boards in quantities:
        p = replace(params, boards=boards, assume_stock=True)
        quotes = quote_all(items, p)
        summary = summarize(items, quotes, p)
        short = sum(1 for q in quotes if q.level != LEVEL_EXCLUDED and q.part is not None and q.buy_qty
                    and q.part.stock is not None and q.part.stock < q.buy_qty)
        unit = (summary.goods / boards).quantize(Decimal("0.0001")) if boards else None
        total_unit = (summary.total / boards).quantize(Decimal("0.0001")) if boards else None
        change = None
        if total_unit is not None and previous:
            change = ((total_unit - previous) / previous * 100).quantize(Decimal("0.1"))
        rows.append(ScenarioRow(boards, summary.currency, summary.goods, unit, summary.total, total_unit,
                                summary.savings, summary.included, summary.priced, summary.unpriced, short, change))
        previous = total_unit if total_unit else previous
    return rows


# --- Modelo rápido para la curva --------------------------------------------------------

@dataclass
class _Option:
    min_qty: int
    mult: int
    breaks: list[tuple[int, float]]  # (cantidad, precio) ordenados
    stock: int | None
    orderable: bool
    lifecycle_rank: int
    recognized: bool
    tolerance: float
    currency: str


@dataclass
class _Entry:
    per_board: int
    pct: Fraction
    options: list[_Option]
    by_spec: bool


@dataclass
class CurvePoint:
    boards: int
    goods: float
    unit: float
    total: float
    total_unit: float
    short: int
    priced: int
    unpriced: int


def _buy_qty(required: int, min_qty: int, mult: int) -> int:
    if required <= 0:
        return 0
    qty = max(required, min_qty or 1)
    step = max(1, mult or 1)
    return -(-qty // step) * step


def _price(option: _Option, qty: int) -> float | None:
    if not option.breaks or qty <= 0:
        return None
    unit = option.breaks[0][1]
    for break_qty, price in option.breaks:
        if break_qty <= qty:
            unit = price
        else:
            break
    return round(unit * qty + 1e-9, 2)


def _option_from_part(part: Part, by_spec: bool) -> _Option:
    breaks = sorted((pb.quantity, float(pb.price)) for pb in part.price_breaks)
    tolerance = candidate_spec(part).tolerance if by_spec else None  # solo desempata pasivos
    return _Option(part.min_qty, part.mult, breaks, part.stock, part.orderable,
                   LEVEL_ORDER.get(part.lifecycle_level, 0), is_recognized(part.manufacturer) if by_spec else False,
                   float(tolerance) if tolerance is not None else 100.0, part.currency)


class CostModel:
    """Réplica en punto flotante de la selección y el costo de `quote.py` (suponiendo stock), para muchas
    cantidades."""

    def __init__(self, items: list[BomItem], params: QuoteParams):
        self.params = params
        self.entries: list[_Entry] = []
        self.currency = ""
        for item in items:
            if not item.include:
                continue
            pct = Fraction(str(params.spares_pct or 0))
            if item.is_passive and params.passive_spares_pct:
                pct = Fraction(str(params.passive_spares_pct))
            pool = self._pool(item)
            by_spec = item.by_spec and item.manual_part is None
            options = [_option_from_part(p, by_spec) for p in pool]
            self.entries.append(_Entry(max(0, item.qty_per_board), pct, options, by_spec))
            for option in options:
                if option.currency and not self.currency:
                    self.currency = option.currency

    @staticmethod
    def _pool(item: BomItem) -> list[Part]:
        if item.manual_part is not None:
            return [item.manual_part]
        if item.by_spec:
            return list(item.candidates)
        pool = list(item.candidates) or list(item.near)
        if item.manufacturer and pool:
            same = [p for p in pool if mfr_match(item.manufacturer, p.manufacturer)]
            if same:
                pool = same
        return pool

    def _required(self, entry: _Entry, boards: int) -> int:
        base = entry.per_board * max(0, boards)
        extra = -(-(base * entry.pct.numerator) // (entry.pct.denominator * 100)) if entry.pct else 0
        return base + max(0, extra)

    def _choose(self, entry: _Entry, required: int) -> _Option | None:
        best_key = None
        best = None
        for option in entry.options:
            qty = _buy_qty(max(required, 1), option.min_qty, option.mult)
            cost = _price(option, qty)
            key = (0 if option.orderable else 1,)  # se supone que habrá stock: no cuenta al elegir
            if entry.by_spec:
                key += (0 if option.recognized else 1,)
            key += (option.lifecycle_rank, cost if cost is not None else math.inf)
            if entry.by_spec:
                key += (option.tolerance,)
            key += (-(option.stock or 0),)
            if best_key is None or key < best_key:
                best_key, best = key, option
        return best

    def _item_cost(self, option: _Option, required: int) -> tuple[int, float] | None:
        base_qty = _buy_qty(required, option.min_qty, option.mult)
        base_cost = _price(option, base_qty)
        if base_cost is None:
            return None
        if not self.params.optimize_breaks:
            return base_qty, base_cost
        best_qty, best_cost = base_qty, base_cost
        for break_qty, _ in option.breaks:
            if break_qty <= base_qty:
                continue
            qty = _buy_qty(break_qty, option.min_qty, option.mult)
            cost = _price(option, qty)
            if cost is not None and cost < best_cost:
                best_qty, best_cost = qty, cost
        return best_qty, best_cost

    def point(self, boards: int) -> CurvePoint:
        goods = 0.0
        short = priced = unpriced = 0
        for entry in self.entries:
            required = self._required(entry, boards)
            if required <= 0:
                continue
            option = self._choose(entry, required)
            if option is None or not option.orderable:
                unpriced += 1
                continue
            result = self._item_cost(option, required)
            if result is None:
                unpriced += 1
                continue
            qty, cost = result
            priced += 1
            goods += cost
            if option.stock is not None and option.stock < qty:
                short += 1
        goods = round(goods, 2)
        p = self.params
        if p.landed and p.import_setup is not None:
            cost = landed_cost(Decimal(str(goods)), self.currency, p.import_setup)
            total = float(cost.total) if cost is not None else goods
        else:
            base = goods + float(p.freight or 0)
            duty = round(base * float(p.duty_pct or 0) / 100, 2)
            vat = round((base + duty) * float(p.vat_pct or 0) / 100, 2)
            total = round(base + duty + vat, 2)
        n = max(1, boards)
        return CurvePoint(boards, goods, goods / n, total, total / n, short, priced, unpriced)

    def curve(self, max_boards: int, extra: list[int] | None = None, samples: int = 100,
              max_break_points: int = 160) -> list[CurvePoint]:
        """Puntos para el gráfico: escala logarítmica + cantidades donde cambia un tramo de precio."""
        max_boards = max(1, int(max_boards))
        quantities = {1, max_boards}
        quantities.update(q for q in (extra or []) if 1 <= q <= max_boards)
        if max_boards > 1:
            for i in range(samples + 1):
                quantities.add(max(1, min(max_boards, round(10 ** (math.log10(max_boards) * i / samples)))))
        breaks = sorted(self.break_points(max_boards))
        if len(breaks) > max_break_points:  # muchos tramos: se reparten parejo en escala logarítmica
            step = len(breaks) / max_break_points
            breaks = [breaks[int(i * step)] for i in range(max_break_points)]
        quantities.update(breaks)
        return [self.point(q) for q in sorted(quantities)]

    def break_points(self, max_boards: int) -> set[int]:
        """Cantidades de placas donde alguna parte cruza un tramo de precio o su mínimo (y la anterior)."""
        points: set[int] = set()
        for entry in self.entries:
            if entry.per_board <= 0:
                continue
            factor = entry.per_board * (1 + entry.pct / 100)
            thresholds = set()
            for option in entry.options:
                thresholds.update(q for q, _ in option.breaks)
                thresholds.add(option.min_qty)
            for threshold in thresholds:
                boards = math.ceil(Fraction(threshold) / factor)
                for b in (boards - 1, boards):
                    if 1 <= b <= max_boards:
                        points.add(int(b))
        return points
