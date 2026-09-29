from datetime import datetime
from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.lookup import lookup_all
from mouser_engine.models import QuoteParams
from mouser_engine.passives import Defaults
from mouser_engine.quote import apply_lookup, queries_for, quote_all, specs_for, summarize
from mouser_engine.report import build_client_report
from mouser_engine.scenarios import scenario_table

QUANTITIES = [1, 10, 25, 50, 100, 500, 1000]


@pytest.fixture
def items(client, example_bom):
    items, _ = build_items(load_table(example_bom))
    specs, required = specs_for(items, QuoteParams(boards=10))
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    return items


def build(items, **params):
    p = QuoteParams(**{"boards": 10, "passive_spares_pct": 10, **params})
    return build_client_report(items, p, QUANTITIES, 1000, client="ACME", project="Placa X",
                               queried_at=datetime(2026, 9, 29, 12, 0))


def test_summary_and_scenarios_match_quote_engine(items):
    report = build(items)
    p = QuoteParams(boards=10, passive_spares_pct=10, assume_stock=True)  # el análisis supone stock
    summary = summarize(items, quote_all(items, p), p)
    assert report.summary.total == summary.total and report.boards == 10
    assert report.params.assume_stock
    assert report.cost_per_board == (summary.total / 10).quantize(Decimal("0.0001"))
    expected = scenario_table(items, p, QUANTITIES)
    assert [(r.boards, r.total, r.total_unit) for r in report.scenarios] == [
        (r.boards, r.total, r.total_unit) for r in expected]
    assert report.cheapest.boards == 1000
    assert report.curve[0].boards == 1 and report.curve[-1].boards == 1000
    assert report.client == "ACME" and report.project == "Placa X" and report.currency == "USD"


def test_lines_and_top_parts(items):
    report = build(items)
    assert len(report.lines) == report.summary.included == 11  # la línea «no montar» no aparece
    shares = sum(line.share for line in report.priced_lines)
    assert abs(shares - 100) < Decimal("0.5")
    assert [line.mpn for line in report.top[:3]] == ["STM32F103C8T6", "BAT54S,215", "ESP32-WROOM-32E-N4"]
    assert report.top == sorted(report.top, key=lambda line: -line.ext_price)
    esp32 = next(line for line in report.lines if line.mpn == "ESP32-WROOM-32E-N4")
    assert esp32.short_from == 500  # 150 en stock: alcanza hasta 100 placas
    missing = next(line for line in report.lines if line.mpn == "XYZ-NOTREAL-1")
    assert missing.ext_price is None and missing.share is None
    passive = next(line for line in report.lines if line.designators == "R11")
    assert passive.by_spec and passive.mpn == "CRCW06034K70FKEA"


def test_observations(items):
    report = build(items)
    by_title = {o.title: o for o in report.observations}
    assert "LM358DR (U2)" in by_title["Stock insuficiente para 10 placas"].items[0]
    assert "no alcanza desde 500 placas" in by_title["Stock que no alcanza para volúmenes mayores"].items[0]
    unpriced = " ".join(by_title["Partes sin precio (no incluidas en los costos)"].items)
    assert "MAX232CPE+" in unpriced and "XYZ-NOTREAL-1" in unpriced and "no se encontró" in unpriced
    assert "3,000" in by_title["Compra mínima mayor a lo necesario"].items[0].replace(".", ",")


def test_notes_describe_what_is_included(items):
    plain = build(items)
    assert any("no incluyen flete" in note for note in plain.notes)
    assert any("cumple o supera" in note for note in plain.notes)  # hay pasivos elegidos por especificación
    extras = build(items, freight=40, vat_pct=19, optimize_breaks=True)
    text = " ".join(extras.notes)
    assert "flete estimado" in text and "IVA de 19 %" in text and "optimización por tramos" in text
    assert extras.summary.total > plain.summary.total


def test_reference_quantity_outside_compared_quantities(items):
    report = build_client_report(items, QuoteParams(boards=37, passive_spares_pct=10), [1, 100], 50,
                                 chart_mode="otro")
    assert report.boards == 37 and report.quantities == [1, 100]
    assert report.max_boards == 100  # el gráfico cubre todas las cantidades
    assert 37 in {p.boards for p in report.curve}
    assert report.chart_mode == "overlay"
    assert report.scenario(37) is None and report.scenario(100) is not None
