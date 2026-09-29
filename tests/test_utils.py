from decimal import Decimal

import pytest

from mouser_engine.formatting import configure, fmt_money, fmt_num, fmt_price
from mouser_engine.models import lead_time_label, packaging_label
from mouser_engine.utils import (
    mfr_match,
    normalize_header,
    normalize_mfr,
    normalize_pn,
    parse_int,
    parse_lead_time_days,
    parse_number,
)


@pytest.mark.parametrize("text,currency,expected", [
    ("$0.10", "USD", "0.10"),
    ("$1,234.50", "USD", "1234.50"),
    ("$1,234", "USD", "1234"),
    ("0,123 €", "EUR", "0.123"),
    ("1.234,50 €", "EUR", "1234.50"),
    ("1 234,50 €", "EUR", "1234.50"),
    ("£0.0045", "GBP", "0.0045"),
    ("¥1,200", "JPY", "1200"),
    ("US$ 0.10", "", "0.10"),
    ("12 In Stock", "", "12"),
    (0.25, "", "0.25"),
    (3, "", "3"),
    # Peso chileno: Mouser usa la convención local (punto de miles, coma decimal)
    ("$1.234", "CLP", "1234"),
    ("$987", "CLP", "987"),
    ("$12,35", "CLP", "12.35"),
    ("$1.234,56", "CLP", "1234.56"),
    ("$1.234.567", "CLP", "1234567"),
    ("$0,123", "CLP", "0.123"),
    ("$0.123", "CLP", "0.123"),  # un 0 no lleva separador de miles
    ("$1,234", "CLP", "1.234"),
    ("$1.234", "USD", "1.234"),
    ("6,85 €", "EUR", "6.85"),
    ("6,85 €", "", "6.85"),
])
def test_parse_number(text, currency, expected):
    assert parse_number(text, currency) == Decimal(expected)


def test_parse_number_decimal_override():
    assert parse_number("$1.234", "", decimal=",") == Decimal("1234")
    assert parse_number("$1.234", "", decimal=".") == Decimal("1.234")
    assert parse_number("$1,234", "", decimal=",") == Decimal("1.234")


@pytest.mark.parametrize("text", [None, "", "N/A", "—"])
def test_parse_number_invalid(text):
    assert parse_number(text) is None


@pytest.mark.parametrize("text,expected", [
    ("10", 10), ("10.0", 10), ("1,234", 1234), ("1.234", 1234), ("1.5", 2),
    ("7483 In Stock", 7483), ("  3 pcs", 3), (8.0, 8),
])
def test_parse_int(text, expected):
    assert parse_int(text) == expected


def test_normalize():
    assert normalize_pn("lm1117imp-3.3/nopb") == "LM1117IMP33NOPB"
    assert normalize_pn("81-GRM188R71C104KA1D") == "81GRM188R71C104KA1D"
    assert normalize_header("  Mfr. Part # ") == "mfr part"
    assert normalize_header("Descripción") == "descripcion"
    assert normalize_header("Nº Parte") == "no parte"


@pytest.mark.parametrize("a,b", [
    ("TI", "Texas Instruments"),
    ("Texas Instruments Inc.", "Texas Instruments"),
    ("ON Semiconductor", "onsemi"),
    ("Murata", "Murata Electronics"),
    ("Vishay Dale", "Vishay"),
    ("Diodes Inc", "Diodes Incorporated"),
    ("Maxim", "Analog Devices / Maxim Integrated"),
    ("Würth Elektronik", "Wurth Elektronik"),
    ("STM", "STMicroelectronics"),
    ("Yageo", "YAGEO"),
])
def test_mfr_match(a, b):
    assert mfr_match(a, b), (normalize_mfr(a), normalize_mfr(b))


@pytest.mark.parametrize("a,b", [("Murata", "Yageo"), ("TI", "Microchip"), ("", "Murata")])
def test_mfr_no_match(a, b):
    assert not mfr_match(a, b)


def test_lead_time():
    assert parse_lead_time_days("56 Days") == 56
    assert parse_lead_time_days("8 Weeks") == 56
    assert parse_lead_time_days("") is None
    assert lead_time_label("6 Weeks") == "6 semanas"
    assert lead_time_label("1 Day") == "1 día"
    assert packaging_label("Reel, Cut Tape, MouseReel") == "Carrete, Cinta cortada, MouseReel"


def test_formatting_chile_and_us():
    try:
        configure(",", ".")
        assert fmt_num(Decimal("1234.5")) == "1.234,50"
        assert fmt_money(Decimal("1234567.891"), "USD") == "USD 1.234.567,89"
        assert fmt_price(Decimal("0.00123")) == "0,00123"
        assert fmt_price(Decimal("7.4")) == "7,40"
        configure(".", ",")
        assert fmt_num(Decimal("-1234.5")) == "-1,234.50"
    finally:
        configure(",", ".")
