"""Funciones de normalización y conversión de texto usadas en todo el proyecto."""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

# Monedas en que Mouser formatea con coma como separador de miles ("$1,234.50").
_COMMA_THOUSANDS_CURRENCIES = {
    "USD", "GBP", "JPY", "CNY", "RMB", "INR", "CAD", "AUD", "NZD", "HKD", "SGD",
    "TWD", "MXN", "ILS", "KRW", "THB", "PHP", "MYR", "ZAR",
}

_NUMBER_RE = re.compile(r"[-+]?\d[\d.,'\s  ]*")


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_header(text: object) -> str:
    """Texto en minúsculas, sin acentos ni puntuación: "Mfr. Part #" -> "mfr part"."""
    if text is None:
        return ""
    s = strip_accents(str(text)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def normalize_pn(text: object) -> str:
    """Número de parte comparable: mayúsculas y solo caracteres alfanuméricos."""
    if text is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", strip_accents(str(text)).upper())


def clean_cell(value: object) -> str:
    """Convierte una celda de planilla a texto limpio ("10.0" -> "10")."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).replace(" ", " ").strip()


def _comma_is_thousands(original: str, currency: str) -> bool:
    if currency and currency.upper() in _COMMA_THOUSANDS_CURRENCIES:
        return True
    return any(sym in original for sym in ("$", "£", "¥", "₹"))


def parse_number(value: object, currency: str = "") -> Decimal | None:
    """Convierte textos como "$1,234.50", "0,123 €", "1.234,5" o "12 In Stock" a Decimal."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return Decimal(repr(value))
    original = str(value).strip()
    match = _NUMBER_RE.search(original)
    if not match:
        return None
    num = re.sub(r"[\s  ']", "", match.group(0)).rstrip(".,")
    if not num or num in "+-":
        return None
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        groups = num.split(",")
        if len(groups) > 2:
            num = num.replace(",", "")
        elif len(groups[1]) == 3 and _comma_is_thousands(original, currency):
            num = num.replace(",", "")
        else:
            num = num.replace(",", ".")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        return Decimal(num)
    except InvalidOperation:
        return None


_GROUPED_INT_RE = re.compile(r"^\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*$")


def parse_int(value: object, default: int | None = None) -> int | None:
    """Entero a partir de texto ("1,234", "1.234", "12 In Stock").

    En contexto de cantidades, "1.234" y "1,234" se leen como separador de miles.
    Si trae decimales reales ("1.5") redondea hacia arriba.
    """
    if isinstance(value, str):
        match = _NUMBER_RE.search(value)
        if match:
            token = re.sub(r"[\s  ']", "", match.group(0)).rstrip(".,")
            if _GROUPED_INT_RE.match(token):
                return int(re.sub(r"[.,]", "", token))
    number = parse_number(value)
    if number is None:
        return default
    return int(number.to_integral_value(rounding="ROUND_CEILING"))


def parse_lead_time_days(text: object) -> int | None:
    """"56 Days" -> 56, "8 Weeks" -> 56, "12 semanas" -> 84."""
    if not text:
        return None
    s = normalize_header(text)
    number = parse_number(s)
    if number is None:
        return None
    n = float(number)
    if re.search(r"week|semana|wk", s):
        n *= 7
    elif re.search(r"month|mes", s):
        n *= 30
    return int(round(n))


# --- Fabricantes -----------------------------------------------------------

_MFR_NOISE_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "llc", "gmbh", "ag", "sa", "spa", "bv", "nv", "plc", "kk", "oy", "ab", "the",
    "semiconductor", "semiconductors", "semi", "electronics", "electronic",
    "technology", "technologies", "tech", "international", "intl", "components",
    "component", "group", "americas", "america", "usa", "industries", "industrial",
    "products", "manufacturing", "mfg",
}

_MFR_ALIASES = {
    "ti": "texas instruments",
    "texas": "texas instruments",
    "texas instrument": "texas instruments",
    "national": "texas instruments",
    "burr brown": "texas instruments",
    "on": "onsemi",
    "on semi": "onsemi",
    "fairchild": "onsemi",
    "st": "stmicroelectronics",
    "stm": "stmicroelectronics",
    "stmicro": "stmicroelectronics",
    "st microelectronics": "stmicroelectronics",
    "freescale": "nxp",
    "philips": "nxp",
    "adi": "analog devices",
    "linear": "analog devices",
    "maxim": "analog devices",
    "maxim integrated": "analog devices",
    "atmel": "microchip",
    "microsemi": "microchip",
    "international rectifier": "infineon",
    "ir": "infineon",
    "cypress": "infineon",
    "epcos": "tdk",
    "tdk epcos": "tdk",
    "te": "te connectivity",
    "tyco": "te connectivity",
    "amp": "te connectivity",
    "we": "wurth elektronik",
    "wurth": "wurth elektronik",
    "samsung": "samsung electro mechanics",
    "semco": "samsung electro mechanics",
    "phycomp": "yageo",
    "diodes": "diodes incorporated",
    "zetex": "diodes incorporated",
    "idt": "renesas",
    "intersil": "renesas",
    "integrated device": "renesas",
    "avx": "kyocera avx",
    "littlefuse": "littelfuse",
    "silabs": "silicon labs",
    "silicon laboratories": "silicon labs",
}


def normalize_mfr(name: object) -> str:
    s = normalize_header(name)
    if not s:
        return ""
    words = [w for w in s.split() if w not in _MFR_NOISE_WORDS]
    s = " ".join(words) if words else s
    return _MFR_ALIASES.get(s) or _MFR_ALIASES.get(s.replace(" ", "")) or s


def mfr_match(a: object, b: object) -> bool:
    """True si dos nombres de fabricante corresponden a la misma empresa."""
    na, nb = normalize_mfr(a), normalize_mfr(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if ca == cb:
        return True
    shorter, longer = sorted((ca, cb), key=len)
    return len(shorter) >= 3 and longer.startswith(shorter)


def chunks(items: list, size: int):
    size = max(1, int(size))
    for start in range(0, len(items), size):
        yield items[start:start + size]
