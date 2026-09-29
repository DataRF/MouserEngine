from dataclasses import replace
from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.landed import ExchangeRates, ImportSetup, builtin_rules
from mouser_engine.models import BomItem, Part, PriceBreak, QuoteParams
from mouser_engine.mouser_api import LookupResult
from mouser_engine.lookup import lookup_all
from mouser_engine.passives import Defaults
from mouser_engine.quote import (
    apply_lookup,
    queries_for,
    quote_all,
    quote_item,
    required_qty,
    select_part,
    specs_for,
    summarize,
)


def part(mouser_pn, mpn="X1", manufacturer="ACME", stock=1000, breaks=((1, "1.00"),), min_qty=1, mult=1,
         lifecycle=""):
    return Part(mouser_pn=mouser_pn, mpn=mpn, manufacturer=manufacturer, stock=stock, min_qty=min_qty,
                mult=mult, lifecycle=lifecycle,
                price_breaks=[PriceBreak(q, Decimal(p), "USD") for q, p in breaks])


def item(**kwargs):
    defaults = dict(id=1, rows=[2], mpn="X1", qty_per_board=1, lookup_state="done")
    defaults.update(kwargs)
    return BomItem(**defaults)


@pytest.fixture
def quoted(client, example_bom):
    items, _ = build_items(load_table(example_bom))
    specs, required = specs_for(items, QuoteParams(boards=10))
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    return items


def test_required_qty_with_spares():
    params = QuoteParams(boards=10, spares_pct=5, passive_spares_pct=10)
    assert required_qty(item(qty_per_board=3), params) == 32  # 30 + ceil(1.5)
    assert required_qty(item(qty_per_board=3, is_passive=True), params) == 33
    assert required_qty(item(qty_per_board=10), QuoteParams(boards=10, spares_pct=7)) == 107  # sin error de float
    assert required_qty(item(qty_per_board=2), QuoteParams(boards=0)) == 0


def test_select_prefers_bom_manufacturer():
    it = item(manufacturer="Vishay", candidates=[part("A", manufacturer="onsemi", breaks=((1, "0.05"),)),
                                                 part("B", manufacturer="Vishay Semiconductors")])
    chosen, flags = select_part(it, 10)
    assert chosen.mouser_pn == "B" and not flags


def test_select_prefers_stock_then_cost():
    cheap_no_stock = part("A", stock=0, breaks=((1, "0.10"),))
    expensive_in_stock = part("B", stock=500, breaks=((1, "0.20"),))
    chosen, _ = select_part(item(candidates=[cheap_no_stock, expensive_in_stock]), 100)
    assert chosen.mouser_pn == "B"


def test_select_reel_only_when_cheaper():
    cut_tape = part("CT", breaks=((1, "0.05"), (100, "0.02")))
    reel = part("REEL", breaks=((4000, "0.008"),), min_qty=4000, mult=4000, stock=100000)
    it = item(candidates=[cut_tape, reel])
    assert select_part(it, 100)[0].mouser_pn == "CT"      # 2.00 vs 32.00
    assert select_part(it, 3500)[0].mouser_pn == "REEL"   # 70.00 vs 32.00


def test_select_flags_ambiguous_and_manual():
    it = item(candidates=[part("A", manufacturer="onsemi"), part("B", manufacturer="Vishay")])
    assert select_part(it, 1)[1] == {"ambiguous": True}
    manual = part("M")
    it.manual_part = manual
    assert select_part(it, 1) == (manual, {"manual": True})


def test_statuses_in_example(quoted):
    params = QuoteParams(boards=10, passive_spares_pct=10)
    by_mpn = {(i.mpn if i.mpn and i.include else f"row{i.rows[0]}"): q
              for i, q in zip(quoted, quote_all(quoted, params))}
    assert by_mpn["GRM188R71C104KA01D"].level == "ok"
    assert by_mpn["GRM188R71C104KA01D"].required == 88
    assert by_mpn["LM358DR"].status == "Sin stock"
    assert by_mpn["1N4148"].status == "Varios fabricantes"
    assert by_mpn["MAX232CPE+"].status == "Sin precio"
    assert any("MAX232ECPE+" in n for n in by_mpn["MAX232CPE+"].notes)
    assert by_mpn["BAT54S,215"].status == "Mínimo de compra alto"
    assert by_mpn["BAT54S,215"].buy_qty == 3000
    assert by_mpn["XYZ-NOTREAL-1"].status == "No encontrado"
    assert by_mpn["row15"].status == "OK · automática"
    assert by_mpn["row16"].level == "excluded"


def test_partial_stock_warning(quoted):
    esp = next(i for i in quoted if i.mpn == "ESP32-WROOM-32E-N4")
    q = quote_item(esp, QuoteParams(boards=200))
    assert q.level == "warn" and q.status == "Stock insuficiente"
    assert any("faltan 50" in n for n in q.notes)
    assert any("En pedido" in n for n in q.notes)


def test_optimization_changes_totals(quoted):
    cap = quoted[0]
    base = quote_item(cap, QuoteParams(boards=11))  # 88 unidades
    assert (base.buy_qty, base.ext_price, base.savings) == (88, Decimal("1.85"), Decimal("0.95"))
    optimized = quote_item(cap, QuoteParams(boards=11, optimize_breaks=True))
    assert (optimized.buy_qty, optimized.ext_price) == (100, Decimal("0.90"))


def test_summary_and_additional_costs(quoted):
    params = QuoteParams(boards=10, freight=40, duty_pct=6, vat_pct=19, fx_rate=950)
    quotes = quote_all(quoted, params)
    s = summarize(quoted, quotes, params)
    assert s.currency == "USD"
    assert s.items == 12 and s.excluded == 1 and s.included == 11
    assert s.priced == 9 and s.unpriced == 2
    assert s.ok + s.warn + s.error + s.pending == 11
    assert s.goods == sum(q.ext_price for q in quotes if q.ext_price is not None and q.level != "excluded")
    base = s.goods + Decimal("40")
    assert s.duty == (base * Decimal("0.06")).quantize(Decimal("0.01"))
    assert s.vat == ((base + s.duty) * Decimal("0.19")).quantize(Decimal("0.01"))
    assert s.total == base + s.duty + s.vat
    assert s.total_clp == (s.total * 950).quantize(Decimal(1))


def test_excluded_items_do_not_count(quoted):
    params = QuoteParams(boards=10)
    before = summarize(quoted, quote_all(quoted, params), params).goods
    stm = next(i for i in quoted if i.mpn == "STM32F103C8T6")
    stm.include = False
    after = summarize(quoted, quote_all(quoted, params), params).goods
    assert before - after == Decimal("67.10")


def test_manual_part_is_refreshed_on_lookup(client):
    it = item(mpn="", lookup_state="noquery")
    it.manual_part = part("603-RC0603FR-074K7L", stock=1)
    assert queries_for([it]) == ["603-RC0603FR-074K7L"]
    apply_lookup([it], client.lookup(queries_for([it]), fuzzy=False))
    assert it.manual_part.stock == 72000
    q = quote_item(it, QuoteParams(boards=10))
    assert q.level == "ok" and q.ext_price == Decimal("0.12")


def test_lookup_error_is_reported():
    it = item(lookup_state="pending")
    apply_lookup([it], {"X1": LookupResult("X1", error="Mouser informó un error: x")})
    q = quote_item(it, QuoteParams())
    assert it.lookup_state == "error" and q.status == "Error de consulta"


def test_clp_account_has_no_conversion_to_clp():
    part = Part.from_api({"MouserPartNumber": "1-A", "ManufacturerPartNumber": "A", "AvailabilityInStock": "100",
                          "PriceBreaks": [{"Quantity": 1, "Price": "$1.000", "Currency": "CLP"}]})
    it = BomItem(id=1, rows=[1], mpn="A", qty_per_board=2, candidates=[part], lookup_state="done")
    params = QuoteParams(boards=3, fx_rate=950)
    summary = summarize([it], quote_all([it], params), params)
    assert summary.currency == "CLP" and summary.goods == Decimal("6000")
    assert summary.total_clp is None  # los precios ya están en pesos


def test_summary_with_landed_cost(quoted):
    setup = ImportSetup(builtin_rules(), ExchangeRates(usd=Decimal(950), customs=Decimal(940)))
    plain = QuoteParams(boards=10, import_setup=setup)
    s = summarize(quoted, quote_all(quoted, plain), plain)
    assert s.landed is None and s.total == s.goods
    assert s.total_clp == (s.total * 950).quantize(Decimal(1))  # dólar observado del día
    params = replace(plain, landed=True)
    s = summarize(quoted, quote_all(quoted, params), params)
    cost = s.landed
    assert cost is not None and s.total == cost.total > s.goods
    assert (s.freight, s.duty, s.vat) == (cost.freight, cost.duty, cost.vat + cost.brokerage_vat)
    assert s.total_clp == cost.total_clp
    no_rules = replace(params, import_setup=None)
    s = summarize(quoted, quote_all(quoted, no_rules), no_rules)
    assert s.landed is None and s.total == s.goods


def test_assume_stock_only_changes_the_choice_when_asked():
    cheap = part("A", stock=5, breaks=((1, "0.50"),))
    stocked = part("B", stock=1000, breaks=((1, "0.80"),))
    it = item(candidates=[cheap, stocked])
    assert quote_item(it, QuoteParams(boards=10)).part.mouser_pn == "B"
    q = quote_item(it, QuoteParams(boards=10, assume_stock=True))
    assert q.part.mouser_pn == "A" and q.level == "warn"  # igual avisa que hoy no alcanza el stock
