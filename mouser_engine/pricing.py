"""Cálculo de cantidades de compra y precios según los tramos de Mouser."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple, Sequence

from .formatting import fmt_int, fmt_price
from .models import PriceBreak

CENT = Decimal("0.01")


class PriceOption(NamedTuple):
    qty: int
    unit_price: Decimal
    ext_price: Decimal
    price_break: PriceBreak


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def purchase_qty(required: int, min_qty: int = 1, mult: int = 1) -> int:
    """Menor cantidad comprable que cubre lo requerido (respeta mínimo y múltiplo)."""
    if required <= 0:
        return 0
    qty = max(int(required), int(min_qty or 1))
    step = max(1, int(mult or 1))
    return -(-qty // step) * step


def break_for_qty(breaks: Sequence[PriceBreak], qty: int) -> PriceBreak | None:
    """Tramo aplicable: el de mayor cantidad que no supera qty."""
    active: PriceBreak | None = None
    for pb in sorted(breaks, key=lambda b: b.quantity):
        if pb.quantity <= qty:
            active = pb
        else:
            break
    if active is None and breaks:
        active = min(breaks, key=lambda b: b.quantity)
    return active


def price_for_qty(breaks: Sequence[PriceBreak], qty: int) -> PriceOption | None:
    """Precio unitario y total para comprar exactamente qty unidades."""
    if not breaks or qty <= 0:
        return None
    pb = break_for_qty(breaks, qty)
    if pb is None:
        return None
    return PriceOption(qty, pb.price, money(pb.price * qty), pb)


def best_option(
    breaks: Sequence[PriceBreak],
    required: int,
    min_qty: int = 1,
    mult: int = 1,
    stock: int | None = None,
) -> tuple[PriceOption | None, PriceOption | None]:
    """Devuelve (compra mínima, compra más barata).

    La compra más barata puede ser mayor que la mínima cuando saltar al siguiente tramo
    de precio reduce el total (p. ej. 100 unidades cuestan menos que 80). No se proponen
    cantidades que superen el stock disponible si la compra mínima sí se puede cubrir.
    """
    base_qty = purchase_qty(required, min_qty, mult)
    base = price_for_qty(breaks, base_qty)
    if base is None:
        return None, None
    best = base
    for pb in sorted(breaks, key=lambda b: b.quantity):
        if pb.quantity <= base_qty:
            continue
        qty = purchase_qty(pb.quantity, min_qty, mult)
        if stock is not None and base_qty <= stock < qty:
            continue
        option = price_for_qty(breaks, qty)
        if option and option.ext_price < best.ext_price:
            best = option
    return base, best


def format_breaks(breaks: Sequence[PriceBreak], limit: int | None = None) -> str:
    """Texto compacto de tramos: "1: 0.10 | 10: 0.08 | 100: 0.05"."""
    shown = list(sorted(breaks, key=lambda b: b.quantity))
    if limit is not None:
        shown = shown[:limit]
    return " | ".join(f"{fmt_int(pb.quantity)}: {fmt_price(pb.price)}" for pb in shown)
