from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.config import DEFAULT_SCENARIOS, parse_quantities
from mouser_engine.lookup import lookup_all
from mouser_engine.models import QuoteParams
from mouser_engine.passives import Defaults
from mouser_engine.quote import apply_lookup, queries_for, quote_all, specs_for, summarize
from mouser_engine.scenarios import CostModel, scenario_table


@pytest.fixture
def items(client, example_bom):
    items, _ = build_items(load_table(example_bom))
    specs, required = specs_for(items, QuoteParams(boards=10))
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    return items


def test_parse_quantities():
    assert parse_quantities(DEFAULT_SCENARIOS) == [1, 10, 25, 50, 100, 500, 1000]
    assert parse_quantities("100, 10; 10  5 x 0 -3 2.5") == [2, 3, 5, 10, 100]
    assert parse_quantities("") == []


def test_scenario_table_matches_quote_engine(items):
    params = QuoteParams(passive_spares_pct=10, freight=25, vat_pct=19)
    rows = scenario_table(items, params, [1, 10, 100])
    for row in rows:
        p = QuoteParams(boards=row.boards, passive_spares_pct=10, freight=25, vat_pct=19)
        summary = summarize(items, quote_all(items, p), p)
        assert row.goods == summary.goods and row.total == summary.total
        assert row.unit == (summary.goods / row.boards).quantize(Decimal("0.0001"))
    assert rows[0].change is None
    assert rows[1].change < 0  # el costo por placa baja con el volumen
    assert rows[-1].short >= 1  # a 100 placas el ESP32 (150 en stock) no alcanza


@pytest.mark.parametrize("optimize", [False, True])
@pytest.mark.parametrize("spares", [0, 7.5])
def test_cost_model_matches_exact_engine(items, optimize, spares):
    base = QuoteParams(spares_pct=spares, passive_spares_pct=10, optimize_breaks=optimize, freight=12.5,
                       duty_pct=6, vat_pct=19)
    model = CostModel(items, base)
    for boards in (1, 2, 3, 7, 9, 10, 11, 25, 88, 100, 101, 150, 333, 500, 1000, 3000):
        p = QuoteParams(boards=boards, spares_pct=spares, passive_spares_pct=10, optimize_breaks=optimize,
                        freight=12.5, duty_pct=6, vat_pct=19)
        quotes = quote_all(items, p)
        summary = summarize(items, quotes, p)
        point = model.point(boards)
        assert point.goods == pytest.approx(float(summary.goods), abs=0.011), boards
        assert point.total == pytest.approx(float(summary.total), abs=0.03), boards
        assert point.priced == summary.priced and point.unpriced == summary.unpriced, boards
        short = sum(1 for q in quotes if q.level != "excluded" and q.part and q.buy_qty
                    and q.part.stock is not None and q.part.stock < q.buy_qty)
        assert point.short == short, boards


def test_curve_includes_scenarios_and_price_break_points(items):
    model = CostModel(items, QuoteParams(passive_spares_pct=10))
    curve = model.curve(1000, extra=[1, 10, 25, 50, 100, 500, 1000])
    boards = [p.boards for p in curve]
    assert boards == sorted(set(boards)) and boards[0] == 1 and boards[-1] == 1000
    assert {10, 25, 50, 100, 500} <= set(boards)
    # el condensador (8 por placa) llega al tramo de 100 unidades a las 13 placas (8 × 13 × 1,1 = 114,4 ≥ 100)
    assert {11, 12} & set(boards)
    units = [p.unit for p in curve]
    assert units[0] > units[-1]


def test_curve_changes_with_parameters(items):
    plain = CostModel(items, QuoteParams()).point(100)
    with_extras = CostModel(items, QuoteParams(freight=100, vat_pct=19)).point(100)
    assert with_extras.total == pytest.approx((plain.goods + 100) * 1.19, abs=0.02)
    assert with_extras.goods == plain.goods
