"""Formato de números para mostrar en pantalla (por defecto, estilo chileno: 1.234,56)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_decimal_sep = ","
_thousands_sep = "."


def configure(decimal_sep: str, thousands_sep: str) -> None:
    """Define los separadores (la interfaz los toma de la configuración regional)."""
    global _decimal_sep, _thousands_sep
    if decimal_sep and decimal_sep != thousands_sep:
        _decimal_sep = decimal_sep
        _thousands_sep = thousands_sep


def separators() -> tuple[str, str]:
    return _decimal_sep, _thousands_sep


def _group(digits: str) -> str:
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return _thousands_sep.join(groups)


def fmt_num(value: object, decimals: int = 2, max_decimals: int | None = None) -> str:
    """Número con separador de miles; entre `decimals` y `max_decimals` decimales."""
    if value is None or value == "":
        return ""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    max_decimals = decimals if max_decimals is None else max(decimals, max_decimals)
    quant = Decimal(1).scaleb(-max_decimals)
    number = number.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if number < 0 else ""
    text = f"{abs(number):f}"
    integer, _, fraction = text.partition(".")
    fraction = fraction.ljust(max_decimals, "0")
    while len(fraction) > decimals and fraction.endswith("0"):
        fraction = fraction[:-1]
    result = sign + _group(integer)
    if fraction:
        result += _decimal_sep + fraction
    return result


def fmt_int(value: object) -> str:
    if value is None or value == "":
        return ""
    return fmt_num(value, 0)


def fmt_money(value: object, currency: str = "") -> str:
    if value is None or value == "":
        return ""
    text = fmt_num(value, 2)
    return f"{currency} {text}".strip()


def fmt_price(value: object, currency: str = "") -> str:
    """Precio unitario: al menos 2 y hasta 5 decimales (los pasivos cuestan fracciones de centavo)."""
    if value is None or value == "":
        return ""
    text = fmt_num(value, 2, 5)
    return f"{currency} {text}".strip()
