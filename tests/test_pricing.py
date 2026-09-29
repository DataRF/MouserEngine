from decimal import Decimal

from mouser_engine.models import PriceBreak
from mouser_engine.pricing import best_option, break_for_qty, price_for_qty, purchase_qty

BREAKS = [
    PriceBreak(1, Decimal("0.10"), "USD"),
    PriceBreak(10, Decimal("0.021"), "USD"),
    PriceBreak(100, Decimal("0.009"), "USD"),
    PriceBreak(1000, Decimal("0.005"), "USD"),
]


def test_purchase_qty_respects_min_and_mult():
    assert purchase_qty(0) == 0
    assert purchase_qty(7) == 7
    assert purchase_qty(7, min_qty=10) == 10
    assert purchase_qty(25, min_qty=10, mult=10) == 30
    assert purchase_qty(24, min_qty=1, mult=5) == 25
    assert purchase_qty(100, min_qty=4000, mult=4000) == 4000
    assert purchase_qty(4001, min_qty=4000, mult=4000) == 8000


def test_break_selection():
    assert break_for_qty(BREAKS, 1).quantity == 1
    assert break_for_qty(BREAKS, 99).quantity == 10
    assert break_for_qty(BREAKS, 100).quantity == 100
    assert break_for_qty(BREAKS, 5000).quantity == 1000
    # cantidad menor al primer tramo: se usa el primero
    assert break_for_qty([PriceBreak(10, Decimal("1"))], 3).quantity == 10


def test_price_for_qty_rounds_to_cents():
    option = price_for_qty(BREAKS, 88)
    assert option.unit_price == Decimal("0.021")
    assert option.ext_price == Decimal("1.85")  # 1.848 -> 1.85
    assert price_for_qty([], 5) is None


def test_best_option_jumps_to_cheaper_tier():
    base, best = best_option(BREAKS, 88)
    assert (base.qty, base.ext_price) == (88, Decimal("1.85"))
    assert (best.qty, best.ext_price) == (100, Decimal("0.90"))


def test_best_option_no_saving():
    base, best = best_option(BREAKS, 150)
    assert base == best


def test_best_option_does_not_exceed_stock():
    base, best = best_option(BREAKS, 88, stock=95)
    assert best.qty == 88


def test_best_option_with_reel_minimum():
    breaks = [PriceBreak(3000, Decimal("0.019")), PriceBreak(9000, Decimal("0.016"))]
    base, best = best_option(breaks, 10, min_qty=3000, mult=3000)
    assert base.qty == 3000 and best.qty == 3000
