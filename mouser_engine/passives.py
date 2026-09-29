"""Resistencias y condensadores sin MPN: lectura de la especificación, verificación de opciones
de Mouser y números de parte candidatos de series comunes.

Flujo:
1. `parse_spec` interpreta lo que dice el BOM (valor, encapsulado, tolerancia, potencia, tensión,
   dieléctrico). Si falta un dato imprescindible, la especificación queda incompleta y no se
   elige nada automáticamente.
2. Las opciones de Mouser (búsqueda por palabra clave y números de parte construidos) se
   evalúan con `evaluate`: deben cumplir o superar cada requisito ("igual o mejor").
3. La selección final (en `quote.py`) prefiere stock suficiente, fabricante reconocido y menor
   costo total para la cantidad requerida.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .models import LEVEL_OK, Part
from .utils import mfr_match, normalize_header

RESISTOR = "resistor"
CAPACITOR = "capacitor"

RECOGNIZED_MANUFACTURERS = [
    "Yageo", "Vishay", "Panasonic", "KOA Speer", "ROHM", "Bourns", "Stackpole", "TE Connectivity",
    "Susumu", "Walsin", "Samsung Electro-Mechanics", "Murata", "TDK", "KEMET", "KYOCERA AVX",
    "Taiyo Yuden", "Würth Elektronik", "Johanson", "Knowles",
]

IMPERIAL_SIZES = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2220", "2512")
METRIC_TO_IMPERIAL = {
    "0402": "01005", "0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805", "3216": "1206",
    "3225": "1210", "4532": "1812", "5025": "2010", "5750": "2220", "6332": "2512",
}
# Potencia mínima típica de una resistencia de película gruesa por tamaño (valor conservador).
STANDARD_RES_POWER = {
    "01005": Decimal("0.03"), "0201": Decimal("0.05"), "0402": Decimal("0.0625"), "0603": Decimal("0.1"),
    "0805": Decimal("0.125"), "1206": Decimal("0.25"), "1210": Decimal("0.33"), "1812": Decimal("0.5"),
    "2010": Decimal("0.5"), "2220": Decimal("0.5"), "2512": Decimal("1"),
}
# Calidad relativa de dieléctricos cerámicos (mayor es mejor / más estable).
DIELECTRIC_RANK = {
    "C0G": 10, "U2J": 8, "X8R": 7, "X8L": 7, "X7R": 6, "X7S": 5, "X7T": 4.5, "X6S": 4.5,
    "X5R": 4, "X5S": 3, "Y5V": 1, "Z5U": 1,
}
_DIELECTRIC_ALIASES = {"COG": "C0G", "NP0": "C0G", "NPO": "C0G", "C0G": "C0G"}
DEFAULT_DIELECTRIC_FLOOR = 4  # sin dato en el BOM: X5R o mejor (se descartan Y5V/Z5U)

_EXCLUDED_WORDS = ("potenciometro", "potentiometer", "trimmer", "trimpot", "array", "network", "red de",
                   "ntc", "ptc", "thermistor", "termistor", "varistor", "fuse", "fusible", "led",
                   "crystal", "cristal", "inductor", "ferrite", "ferrita", "choke", "bobina")
_OTHER_CAP_TYPES = {
    "electrolitico": "electrolítico", "electrolytic": "electrolítico", "elco": "electrolítico",
    "aluminum": "electrolítico", "aluminio": "electrolítico", "tantal": "tántalo",
    "polymer": "polímero", "polimero": "polímero", "film": "film", "polyester": "film",
    "polypropylene": "film", "polipropileno": "film", "mkt": "film", "mkp": "film",
    "supercap": "supercondensador", "edlc": "supercondensador",
}


class Defaults:
    """Valores usados cuando el BOM no indica un dato (vienen de la configuración)."""

    def __init__(self, res_tolerance: float = 5.0, cap_tolerance: float = 20.0, cap_voltage: float = 16.0):
        self.res_tolerance = Decimal(str(res_tolerance))
        self.cap_tolerance = Decimal(str(cap_tolerance))
        self.cap_voltage = Decimal(str(cap_voltage))


@dataclass(frozen=True)
class PassiveSpec:
    kind: str
    value: Decimal | None  # ohm o faradios
    package: str | None  # tamaño imperial ("0603")
    tolerance: Decimal | None = None  # %
    power: Decimal | None = None  # W
    voltage: Decimal | None = None  # V
    dielectric: str | None = None
    tempco: int | None = None  # ppm/°C
    cap_type: str = "cerámico"
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def key(self) -> str:
        return "|".join(str(x) for x in (self.kind, self.value, self.package, self.tolerance, self.power,
                                         self.voltage, self.dielectric, self.tempco, self.cap_type))

    def label(self) -> str:
        parts = ["Resistencia" if self.kind == RESISTOR else "Condensador"]
        if self.value is not None:
            parts.append(format_resistance(self.value) if self.kind == RESISTOR else format_capacitance(self.value))
        if self.tolerance is not None:
            parts.append(f"±{_fmt_dec(self.tolerance)} %")
        if self.voltage is not None:
            parts.append(f"{_fmt_dec(self.voltage)} V")
        if self.dielectric:
            parts.append(self.dielectric)
        if self.power is not None:
            parts.append(format_power(self.power))
        if self.tempco is not None:
            parts.append(f"{self.tempco} ppm/°C")
        if self.package:
            parts.append(self.package)
        return " ".join(parts)


# --- Formato -------------------------------------------------------------------

def _fmt_dec(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text.replace(".", ",")


def format_resistance(ohms: Decimal) -> str:
    if ohms >= 1_000_000:
        return f"{_fmt_dec(ohms / 1_000_000)} MΩ"
    if ohms >= 1000:
        return f"{_fmt_dec(ohms / 1000)} kΩ"
    return f"{_fmt_dec(ohms)} Ω"


def format_capacitance(farads: Decimal) -> str:
    pf = farads * Decimal("1e12")
    if pf >= 1_000_000:
        return f"{_fmt_dec(pf / 1_000_000)} µF"
    if pf >= 1000:
        return f"{_fmt_dec(pf / 1000)} nF"
    return f"{_fmt_dec(pf)} pF"


def format_power(watts: Decimal) -> str:
    for denominator in (8, 10, 16, 20, 4, 3, 2):
        if watts * denominator == 1:
            return f"1/{denominator} W"
    return f"{_fmt_dec(watts)} W"


# --- Números en texto ------------------------------------------------------------

def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


_RES_MULT = {"R": Decimal(1), "K": Decimal(1000), "M": Decimal(1_000_000), "G": Decimal(1_000_000_000)}
_RKM_RES_RE = re.compile(r"^(\d*)([RrKkMG])(\d*)$")
_RES_UNIT_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(k|K|M|G|m|meg|Meg)?\s*(?:Ω|ohms?|ohmios?|ohm)(?![a-z])",
                          re.IGNORECASE)
_RES_PREFIX_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(k|K|M|G)(?![\w])")


def _res_prefix(prefix: str | None, text_after_number: str = "") -> Decimal | None:
    if not prefix:
        return Decimal(1)
    if prefix in ("k", "K"):
        return Decimal(1000)
    if prefix in ("M", "meg", "Meg", "MEG"):
        return Decimal(1_000_000)
    if prefix in ("G",):
        return Decimal(1_000_000_000)
    if prefix == "m":
        return Decimal("0.001")
    return None


def parse_resistance(text: str, bare_numbers: bool = False) -> Decimal | None:
    """Ohm a partir de "10k", "4K7", "100R", "0R", "10 kΩ", "2.2 Mohm", "470" (si bare_numbers)."""
    if not text:
        return None
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)  # coma decimal: "4,7k"
    for token in re.split(r"[\s,;/()\[\]]+", text.strip()):
        match = _RKM_RES_RE.match(token)
        if match and (match.group(1) or match.group(3)):
            whole, letter, frac = match.groups()
            if letter == "k":
                letter = "K"
            if letter == "r":
                letter = "R"
            number = Decimal(f"{whole or '0'}.{frac or '0'}")
            return number * _RES_MULT[letter]
    match = _RES_UNIT_RE.search(text)
    if match:
        number = _dec(match.group(1))
        prefix = match.group(2)
        mult = _res_prefix(prefix)
        if number is not None and mult is not None:
            return number * mult
    match = _RES_PREFIX_RE.search(text)
    if match:
        number = _dec(match.group(1))
        mult = _res_prefix(match.group(2))
        if number is not None and mult is not None:
            return number * mult
    if bare_numbers:
        match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*", text)
        if match:
            return _dec(match.group(1))
    return None


_CAP_MULT = {"p": Decimal("1e-12"), "n": Decimal("1e-9"), "u": Decimal("1e-6"), "µ": Decimal("1e-6"),
             "μ": Decimal("1e-6"), "m": Decimal("1e-3")}
_NUM = r"(\d+(?:[.,]\d+)?|[.,]\d+)"
_CAP_UNIT_RE = re.compile(r"(?<![\w.,])" + _NUM + r"\s*([pnuµμ])\s*F?(?![a-zA-Z])|(?<![\w.,])" + _NUM + r"\s*(m)F\b")
_CAP_RKM_RE = re.compile(r"(?<![\w.,])(\d+)([pnuµμ])(\d+)(?![\w])")


def parse_capacitance(text: str) -> Decimal | None:
    """Faradios a partir de "100nF", "0.1uF", "4u7", "2n2", "10 pF" (la unidad es obligatoria)."""
    if not text:
        return None
    match = _CAP_RKM_RE.search(text)
    if match:
        whole, prefix, frac = match.groups()
        return Decimal(f"{whole}.{frac}") * _CAP_MULT[prefix]
    match = _CAP_UNIT_RE.search(text)
    if match:
        if match.group(1):
            number = _dec(match.group(1))
            return number * _CAP_MULT[match.group(2)] if number is not None else None
        number = _dec(match.group(3))
        return number * _CAP_MULT["m"] if number is not None else None
    return None


_TOL_RE = re.compile(r"(?:±|\+/-|\+-)?\s*(\d+(?:[.,]\d+)?)\s*%")
_TOL_PF_RE = re.compile(r"±\s*(\d+(?:[.,]\d+)?)\s*pF", re.IGNORECASE)
_POWER_FRAC_RE = re.compile(r"(?<![\d.])(\d+)\s*/\s*(\d+)\s*(?:W\b|watts?\b|vatios?\b)", re.IGNORECASE)
_POWER_RE = re.compile(r"(?<![\w.,/])(\d+(?:[.,]\d+)?)\s*(m)?\s*(?:W\b|watts?\b|vatios?\b)", re.IGNORECASE)
_VOLT_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(k)?\s*(?:V\b|VDC\b|Vdc\b|V\s*DC\b|volts?\b|voltios?\b)",
                      re.IGNORECASE)
_VOLT_RKM_RE = re.compile(r"(?<![\w.,])(\d+)V(\d+)(?![\w])")
_DIELECTRIC_RE = re.compile(r"(?<![A-Z0-9])(C0G|COG|NP0|NPO|X7R|X5R|X7S|X6S|X8R|X8L|X7T|X5S|Y5V|Z5U|U2J)(?![A-Z0-9])",
                            re.IGNORECASE)
_TEMPCO_RE = re.compile(r"(\d+)\s*ppm", re.IGNORECASE)


_TOL_ASYM_RE = re.compile(r"\+\s*(\d+(?:[.,]\d+)?)\s*%?\s*(?:/\s*)?-\s*(\d+(?:[.,]\d+)?)\s*%")


def parse_tolerance(text: str) -> Decimal | None:
    """Tolerancia en %: "±1%", "5 %". Las asimétricas ("+80-20%") cuentan por su peor lado."""
    match = _TOL_ASYM_RE.search(text or "")
    if match:
        values = [v for v in (_dec(match.group(1)), _dec(match.group(2))) if v is not None]
        return max(values) if values else None
    match = _TOL_RE.search(text or "")
    return _dec(match.group(1)) if match else None


def parse_power(text: str) -> Decimal | None:
    match = _POWER_FRAC_RE.search(text or "")
    if match:
        denominator = Decimal(match.group(2))
        if denominator:
            return Decimal(match.group(1)) / denominator
    match = _POWER_RE.search(text or "")
    if match:
        number = _dec(match.group(1))
        if number is None:
            return None
        return number / 1000 if match.group(2) else number
    return None


def parse_voltage(text: str) -> Decimal | None:
    match = _VOLT_RKM_RE.search(text or "")
    if match:
        return Decimal(f"{match.group(1)}.{match.group(2)}")
    match = _VOLT_RE.search(text or "")
    if match:
        number = _dec(match.group(1))
        if number is None:
            return None
        return number * 1000 if match.group(2) else number
    return None


def parse_dielectric(text: str) -> str | None:
    match = _DIELECTRIC_RE.search(text or "")
    if not match:
        return None
    token = match.group(1).upper()
    return _DIELECTRIC_ALIASES.get(token, token)


def parse_tempco(text: str) -> int | None:
    match = _TEMPCO_RE.search(text or "")
    return int(match.group(1)) if match else None


_IMPERIAL_RE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)(?!\d)")
_IPC_METRIC_RE = re.compile(r"(?:RESC|CAPC|RESM|CAPM|RC|CC)(0402|0603|1005|1608|2012|3216|3225|4532|5025|5750|6332)X",
                            re.IGNORECASE)
_METRIC_WORD_RE = re.compile(r"(?<!\d)(0402|0603|1005|1608|2012|3216|3225|4532|5025|5750|6332)\s*[-_]?\s*(?:metric|métrico|metrico|m\b)",
                             re.IGNORECASE)


def parse_package(*texts: str) -> str | None:
    """Tamaño imperial ("0603") desde footprints de KiCad/Altium/Fusion, nombres IPC o texto libre."""
    for text in texts:
        if not text:
            continue
        match = _IPC_METRIC_RE.search(text)
        if match:
            return METRIC_TO_IMPERIAL.get(match.group(1))
        cleaned = _METRIC_WORD_RE.sub(" ", text)  # "0603_1608Metric": ignora la parte métrica
        match = _IMPERIAL_RE.search(cleaned)
        if match:
            return match.group(1)
        match = _METRIC_WORD_RE.search(text)
        if match:
            return METRIC_TO_IMPERIAL.get(match.group(1))
    return None


# --- Especificación desde el BOM -----------------------------------------------------

def _designator_prefixes(designators: str) -> set[str]:
    prefixes = set()
    for token in re.split(r"[,;\s/]+", designators or ""):
        match = re.match(r"([A-Za-z]+)\d", token)
        if match:
            prefixes.add(match.group(1).upper())
    return prefixes


def detect_kind(designators: str, text: str) -> str | None:
    lowered = normalize_header(text)
    if any(f" {word} " in f" {lowered} " for word in _EXCLUDED_WORDS):
        return None
    prefixes = _designator_prefixes(designators)
    if prefixes == {"R"}:
        return RESISTOR
    if prefixes == {"C"}:
        return CAPACITOR
    if prefixes and not prefixes <= {"R", "C"}:
        return None
    if re.search(r"\b(resistor|resistencia|resistance|res)\b", lowered) or re.search(r"\d\s*(?:Ω|ohm)", text or "", re.I):
        return RESISTOR
    if re.search(r"\b(capacitor|condensador|capacitancia|cap|mlcc)\b", lowered) or parse_capacitance(text or ""):
        return CAPACITOR
    return None


def parse_spec(designators: str = "", value: str = "", description: str = "", footprint: str = "",
               extra: dict[str, str] | None = None) -> PassiveSpec | None:
    """Especificación de una resistencia o condensador. None si la línea no es de esos tipos."""
    extra_text = " ".join(f"{k} {v}" for k, v in (extra or {}).items() if v)
    all_text = " ".join(x for x in (value, description, footprint, extra_text) if x)
    kind = detect_kind(designators, all_text)
    if kind is None:
        return None
    spec_text = " ".join(x for x in (value, description, extra_text) if x)
    missing = []
    package = parse_package(footprint, value, description, extra_text)
    if package is None:
        missing.append("encapsulado")
    tolerance = parse_tolerance(spec_text)
    if kind == RESISTOR:
        ohms = parse_resistance(value, bare_numbers=True) if value else None
        if ohms is None:
            ohms = parse_resistance(" ".join(x for x in (description, extra_text) if x))
        if ohms is None:
            missing.append("valor en ohm")
        return PassiveSpec(
            kind=RESISTOR, value=ohms, package=package, tolerance=tolerance,
            power=parse_power(spec_text), tempco=parse_tempco(spec_text), missing=tuple(missing))
    farads = parse_capacitance(value) if value else None
    if farads is None:
        farads = parse_capacitance(" ".join(x for x in (description, extra_text) if x))
    if farads is None:
        missing.append("capacidad (con unidad: pF, nF o µF)")
    cap_type = "cerámico"
    lowered = normalize_header(all_text)
    for word, name in _OTHER_CAP_TYPES.items():
        if word in lowered:
            cap_type = name
            break
    if cap_type != "cerámico":
        missing.append(f"tipo {cap_type} (la elección automática solo cubre cerámicos)")
    return PassiveSpec(
        kind=CAPACITOR, value=farads, package=package, tolerance=tolerance, voltage=parse_voltage(spec_text),
        dielectric=parse_dielectric(all_text), cap_type=cap_type, missing=tuple(missing))


# --- Especificación de una parte de Mouser ------------------------------------------------

_MPN_PACKAGE_RULES: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"^(?:GRM|GCM|GCJ|GRT|GJM|GQM|GCQ|GMA|GRJ)(\d{2})"),
     {"03": "0201", "15": "0402", "18": "0603", "21": "0805", "31": "1206", "32": "1210", "43": "1812", "55": "2220"}),
    (re.compile(r"^CL(\d{2})[A-Z]"), {"03": "0201", "05": "0402", "10": "0603", "21": "0805", "31": "1206", "32": "1210"}),
    (re.compile(r"^CGA(\d)"), {"1": "0201", "2": "0402", "3": "0603", "4": "0805", "5": "1206", "6": "1210"}),
    (re.compile(r"^[A-Z]{3}(063|105|107|212|316|325)"),
     {"063": "0201", "105": "0402", "107": "0603", "212": "0805", "316": "1206", "325": "1210"}),
    (re.compile(r"^ERJ-?(1G|2R|2G|3E|3G|6E|6G|8E|8G|14|12|1T|P03|P06|P08|PA3|PA6|PA8|U02|U03|U06|U08)"),
     {"1G": "0201", "2R": "0402", "2G": "0402", "3E": "0603", "3G": "0603", "6E": "0805", "6G": "0805",
      "8E": "1206", "8G": "1206", "14": "1210", "12": "1812", "1T": "2512", "P03": "0603", "P06": "0805",
      "P08": "1206", "PA3": "0603", "PA6": "0805", "PA8": "1206", "U02": "0402", "U03": "0603",
      "U06": "0805", "U08": "1206"}),
    (re.compile(r"^RK73[HBGZ](1E|1J|2A|2B|2E|2H|3A)"),
     {"1E": "0402", "1J": "0603", "2A": "0805", "2B": "1206", "2E": "1210", "2H": "2010", "3A": "2512"}),
    (re.compile(r"^(?:MCR|ESR|KTR|SFR|UCR|LTR)(01|03|10|18|25|50|100)"),
     {"01": "0402", "03": "0603", "10": "0805", "18": "1206", "25": "1210", "50": "2010", "100": "2512"}),
]
# Series que escriben el tamaño imperial justo después del prefijo (RC0603…, CRCW0603…, CC0603…).
_IMPERIAL_PREFIX_RE = re.compile(
    r"^(?:CRCW|RMCF|RNCP|TNPW|CRGCQ|CRG|CR|RC|AC|RT|AT|RL|CC|AF|VJ)[-_]?"
    r"(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)")


def mpn_package(mpn: str, manufacturer: str = "") -> str | None:
    """Tamaño imperial deducido del número de parte de series conocidas."""
    code = (mpn or "").upper().replace(" ", "")
    if not code:
        return None
    if mfr_match(manufacturer, "Samsung Electro-Mechanics") and code.startswith("RC"):
        match = re.match(r"^RC(0603|1005|1608|2012|3216|3225|5025|6432)", code)
        return METRIC_TO_IMPERIAL.get(match.group(1)) if match else None
    # KEMET (imperial): C0603C104K4RACTU -> tamaño + "C" + código de capacidad
    match = re.match(r"^C(0201|0402|0603|0805|1206|1210|1812|2220)C(?:\d{3}|\dR\d)[A-Z]", code)
    if match:
        return match.group(1)
    # TDK (métrico): C1608X7R1C104K080AA -> tamaño + dieléctrico
    match = re.match(r"^C(0603|1005|1608|2012|3216|3225|4532|5750)(?:X\d[A-Z]|C0G|CH|JB|NP0|U2J)", code)
    if match:
        return METRIC_TO_IMPERIAL.get(match.group(1))
    for pattern, table in _MPN_PACKAGE_RULES:
        match = pattern.match(code)
        if match and match.group(1) in table:
            return table[match.group(1)]
    match = re.match(r"^(01005|0201|0402|0603|0805|1206|1210|1812|2220)[0-9A-Z]", code)
    if match:  # KYOCERA AVX (06035C104KAT2A), Walsin (0603B104K500CT)
        return match.group(1)
    match = _IMPERIAL_PREFIX_RE.match(code)
    if match:
        return match.group(1)
    return None


@dataclass
class CandidateSpec:
    kind: str | None
    value: Decimal | None
    package: str | None
    tolerance: Decimal | None
    power: Decimal | None
    voltage: Decimal | None
    dielectric: str | None
    tempco: int | None
    jumper: bool = False


_RES_CATEGORY_EXCLUDE = ("network", "array", "potentiometer", "trimmer", "thermistor", "varistor", "fuse",
                         "through hole", "leaded", "melf")
_CAP_CATEGORY_EXCLUDE = ("leaded", "through hole", "array", "trimmer", "feed through", "high voltage")


def _mouser_resistance(description: str) -> tuple[Decimal | None, bool]:
    if re.search(r"\bjumper\b|\bzero ohm\b", description, re.IGNORECASE):
        return Decimal(0), True
    match = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(k|K|M|G|m)?\s*ohms?\b", description)
    if not match:
        match = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(k|K|M|G|m)?\s*Ohms?\b", description, re.IGNORECASE)
    if not match:
        return None, False
    number = Decimal(match.group(1))
    mult = _res_prefix(match.group(2))
    value = number * mult if mult is not None else None
    return value, value == 0


def candidate_spec(part: Part) -> CandidateSpec:
    """Lee tipo, valor, tamaño y demás datos de una parte de Mouser (categoría, descripción y MPN)."""
    description = part.description or ""
    category = f"{part.category} {description.split(' - ')[0] if ' - ' in description else ''}".lower()
    text = f"{description} {part.category}"
    kind = None
    if "resistor" in category and not any(word in category for word in _RES_CATEGORY_EXCLUDE):
        kind = RESISTOR
    elif ("ceramic capacitor" in category or "mlcc" in category) and not any(
            word in category for word in _CAP_CATEGORY_EXCLUDE):
        kind = CAPACITOR
    package = None
    match = _IMPERIAL_RE.search(description)
    if match:
        package = match.group(1)
    package = package or mpn_package(part.mpn, part.manufacturer)
    if kind == RESISTOR:
        value, jumper = _mouser_resistance(description)
        tolerance = parse_tolerance(description)
        return CandidateSpec(RESISTOR, value, package, tolerance, parse_power(description), None, None,
                             parse_tempco(description), jumper)
    if kind == CAPACITOR:
        value = parse_capacitance(description)
        tolerance = parse_tolerance(description)
        if tolerance is None and value:
            match = _TOL_PF_RE.search(description)
            if match:
                tolerance = _dec(match.group(1)) * Decimal("1e-12") / value * 100
        return CandidateSpec(CAPACITOR, value, package, tolerance, None, parse_voltage(description),
                             parse_dielectric(text), None)
    return CandidateSpec(None, None, package, None, None, None, None, None)


# --- Evaluación "igual o mejor" -----------------------------------------------------------

@dataclass
class Evaluation:
    ok: bool
    reason: str = ""  # motivo de descarte (corto)
    candidate: CandidateSpec | None = None
    recognized: bool = False
    assumptions: list[str] = field(default_factory=list)


def is_recognized(manufacturer: str) -> bool:
    return any(mfr_match(manufacturer, name) for name in RECOGNIZED_MANUFACTURERS)


def requirements(spec: PassiveSpec, defaults: Defaults) -> tuple[dict, list[str]]:
    """Requisitos efectivos (con valores por defecto) y notas de lo que se asumió."""
    req: dict = {}
    notes: list[str] = []
    if spec.kind == RESISTOR:
        if spec.value != 0:
            if spec.tolerance is not None:
                req["tolerance"] = spec.tolerance
            else:
                req["tolerance"] = defaults.res_tolerance
                notes.append(f"Tolerancia no indicada en el BOM: se exigió ±{_fmt_dec(defaults.res_tolerance)} % o mejor.")
        if spec.power is not None:
            req["power"] = spec.power
        if spec.tempco is not None:
            req["tempco"] = spec.tempco
    else:
        if spec.tolerance is not None:
            req["tolerance"] = spec.tolerance
        else:
            req["tolerance"] = defaults.cap_tolerance
            notes.append(f"Tolerancia no indicada en el BOM: se exigió ±{_fmt_dec(defaults.cap_tolerance)} % o mejor.")
        if spec.voltage is not None:
            req["voltage"] = spec.voltage
        else:
            req["voltage"] = defaults.cap_voltage
            notes.append(f"Tensión no indicada en el BOM: se exigió {_fmt_dec(defaults.cap_voltage)} V o más. "
                         "Confírmela con el diseño.")
        if spec.dielectric:
            req["dielectric_rank"] = DIELECTRIC_RANK.get(spec.dielectric, DEFAULT_DIELECTRIC_FLOOR)
        else:
            req["dielectric_rank"] = DEFAULT_DIELECTRIC_FLOOR
            notes.append("Dieléctrico no indicado en el BOM: se aceptó X5R o mejor (se descartan Y5V/Z5U).")
    return req, notes


def evaluate(spec: PassiveSpec, part: Part, defaults: Defaults) -> Evaluation:
    """¿La parte de Mouser cumple o supera la especificación? Devuelve el motivo si no."""
    cand = candidate_spec(part)
    recognized = is_recognized(part.manufacturer)
    req, notes = requirements(spec, defaults)

    def reject(reason: str) -> Evaluation:
        return Evaluation(False, reason, cand, recognized, notes)

    if cand.kind != spec.kind:
        return reject("otro tipo de componente")
    if cand.value is None:
        return reject("valor no verificable")
    if spec.value is None or cand.value != spec.value:
        return reject("valor distinto")
    if cand.package is None:
        return reject("encapsulado no verificable")
    if cand.package != spec.package:
        return reject("encapsulado distinto")
    if part.lifecycle_level != LEVEL_OK:
        return reject("obsoleta o no recomendada")
    if not part.orderable:
        return reject("sin precio")
    if "tolerance" in req and not cand.jumper:
        if cand.tolerance is None:
            return reject("tolerancia no verificable")
        if cand.tolerance > req["tolerance"]:
            return reject("tolerancia insuficiente")
    if spec.kind == RESISTOR:
        if "power" in req:
            power = cand.power
            if power is None:
                power = STANDARD_RES_POWER.get(cand.package)
            if power is None or power < req["power"]:
                return reject("potencia insuficiente")
        if "tempco" in req and not cand.jumper:
            if cand.tempco is None:
                return reject("coeficiente de temperatura no verificable")
            if cand.tempco > req["tempco"]:
                return reject("coeficiente de temperatura insuficiente")
    else:
        if cand.voltage is None:
            return reject("tensión no verificable")
        if cand.voltage < req["voltage"]:
            return reject("tensión insuficiente")
        if cand.dielectric is None:
            return reject("dieléctrico no verificable")
        rank = DIELECTRIC_RANK.get(cand.dielectric, 0)
        if rank < req["dielectric_rank"]:
            return reject("dieléctrico inferior")
        if spec.dielectric == "C0G" and cand.dielectric != "C0G":
            return reject("dieléctrico inferior")
    return Evaluation(True, "", cand, recognized, notes)


# --- Números de parte candidatos de series comunes -------------------------------------------

def _digits(value: Decimal, significant: int) -> tuple[str, int]:
    """Mantisa con `significant` dígitos y exponente: 10000 -> ("100", 2) para 3 dígitos."""
    if value <= 0:
        return "0" * significant, 0
    exponent = value.adjusted() - (significant - 1)
    mantissa = (value / (Decimal(10) ** exponent)).quantize(Decimal(1))
    if mantissa >= Decimal(10) ** significant:  # redondeo que agrega un dígito
        exponent += 1
        mantissa = (value / (Decimal(10) ** exponent)).quantize(Decimal(1))
    if value != mantissa * (Decimal(10) ** exponent):
        raise ValueError("el valor no cabe en el código")
    return str(int(mantissa)).zfill(significant), exponent


def eia_code(ohms: Decimal, significant: int) -> str | None:
    """Código EIA de resistencias: 3 dígitos (5 %) o 4 dígitos (1 %); "R" para decimales."""
    try:
        if ohms < Decimal(10) ** (significant - 1):
            text = f"{ohms.normalize():f}"
            whole, _, frac = text.partition(".")
            code = f"{whole}R{frac}" if whole != "0" else f"R{frac}"
            if len(code.replace("R", "")) > significant:
                return None
            return code.ljust(significant + 1, "0")[:significant + 1]
        mantissa, exponent = _digits(ohms, significant)
        if exponent > 9:
            return None
        return f"{mantissa}{exponent}"
    except (ValueError, InvalidOperation):
        return None


def rkm_code(ohms: Decimal, width: int | None = None) -> str | None:
    """Código RKM: 10k -> "10K", 4,7k -> "4K7", 100 -> "100R". Con width fija el largo (10K0)."""
    for letter, mult in (("M", Decimal(1_000_000)), ("K", Decimal(1000)), ("R", Decimal(1))):
        if ohms >= mult or letter == "R":
            scaled = ohms / mult
            text = f"{scaled.normalize():f}"
            whole, _, frac = text.partition(".")
            if whole == "0":
                whole = "0" if letter == "R" and not frac else whole
            code = f"{whole}{letter}{frac}"
            if width is not None:
                if len(code) > width:
                    return None
                code = code.ljust(width, "0")
            return code
    return None


def _cap_code(farads: Decimal) -> str | None:
    """Código EIA de capacidad en pF: 100 nF -> "104", 22 pF -> "220"."""
    pf = farads * Decimal("1e12")
    if pf < 10:
        return None
    try:
        mantissa, exponent = _digits(pf, 2)
    except (ValueError, InvalidOperation):
        return None
    if exponent > 7:
        return None
    return f"{mantissa}{exponent}"


def constructed_candidates(spec: PassiveSpec) -> list[str]:
    """Números de parte plausibles en series comunes de fabricantes reconocidos.

    Muchos no existirán; la búsqueda exacta en Mouser descarta los que no existen y la
    evaluación verifica los que sí.
    """
    if not spec.complete or spec.value is None or spec.package is None:
        return []
    pkg = spec.package
    out: list[str] = []
    if spec.kind == RESISTOR:
        ohms = spec.value
        tol = spec.tolerance
        tolerances = []
        if tol is None or tol >= 1:
            tolerances.append("F")
        if tol is None or tol >= 5:
            tolerances.append("J")
        if ohms == 0:
            out += [f"RC{pkg}JR-070RL", f"CRCW{pkg}0000Z0EA", f"RMCF{pkg}ZT0R00"]
            if pkg in _ERJ_SIZE:
                out.append(f"ERJ-{_ERJ_SIZE[pkg]}GE0R00X" if pkg == "0402" else f"ERJ-{_ERJ_SIZE[pkg]}GEY0R00V")
            return out
        rkm = rkm_code(ohms)
        four = rkm_code(ohms, width=4)
        eia4 = eia_code(ohms, 3)
        eia3 = eia_code(ohms, 2)
        for t in tolerances:
            if rkm and pkg in ("0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512"):
                out.append(f"RC{pkg}{t}R-07{rkm}L")
            if four and pkg in ("0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512"):
                reel = "ED" if pkg in ("0201", "0402") else "EA"
                out.append(f"CRCW{pkg}{four}{t}{'K' if t == 'F' else 'N'}{reel}")
                out.append(f"RMCF{pkg}{t}T{four}")
            if pkg in _ERJ_SIZE:
                if t == "F" and eia4:
                    out.append(_erj_1pct(pkg, eia4))
                if t == "J" and eia3:
                    out.append(_erj_5pct(pkg, eia3))
            if pkg in _KOA_SIZE:
                tape = "TTP" if pkg == "0402" else "TTD"
                if t == "F" and eia4:
                    out.append(f"RK73H{_KOA_SIZE[pkg]}{tape}{eia4}F")
                if t == "J" and eia3:
                    out.append(f"RK73B{_KOA_SIZE[pkg]}{tape}{eia3}J")
            if pkg in ("0402", "0603", "0805", "1206"):
                suffix = "GLF" if pkg == "0402" else "ELF"
                if t == "F" and eia4:
                    out.append(f"CR{pkg}-FX-{eia4}{suffix}")
                if t == "J" and eia3:
                    out.append(f"CR{pkg}-JW-{eia3}{suffix}")
        return list(dict.fromkeys(out))

    # Condensadores cerámicos: KEMET (C0603C104K4RACTU) y Yageo (CC0603KRX7R9BB104)
    code = _cap_code(spec.value)
    if code is None or pkg not in ("0402", "0603", "0805", "1206", "1210"):
        return []
    floor = spec.voltage if spec.voltage is not None else Decimal(16)
    voltages = [v for v in (Decimal("6.3"), Decimal(10), Decimal(16), Decimal(25), Decimal(50), Decimal(100))
                if v >= floor][:3]
    if spec.dielectric == "C0G":
        dielectrics = ["C0G"]
    elif spec.dielectric in ("X7R", "X8R", "X8L", "U2J"):
        dielectrics = ["X7R"] if spec.dielectric == "X7R" else []
    else:  # sin dato, X5R o inferiores: X7R y X5R cumplen
        dielectrics = ["X7R", "X5R"]
    tol = spec.tolerance if spec.tolerance is not None else Decimal(20)
    tapings = ("R",) if pkg in ("0402", "0603") else ("R", "K")
    for diel in dielectrics:
        if diel == "C0G":
            tol_codes = ["J"] if tol >= 5 else []
        else:
            tol_codes = ["K"] if tol >= 10 else []
        for volt in voltages:
            for t in tol_codes:
                kemet_v = _KEMET_VOLT.get(volt)
                kemet_d = _KEMET_DIEL.get(diel)
                if kemet_v and kemet_d:
                    out.append(f"C{pkg}C{code}{t}{kemet_v}{kemet_d}ACTU")
                yageo_v = _YAGEO_VOLT.get(volt)
                if yageo_v:
                    yageo_d, series = ("NPO", "BN") if diel == "C0G" else (diel, "BB")
                    for taping in tapings:
                        out.append(f"CC{pkg}{t}{taping}{yageo_d}{yageo_v}{series}{code}")
    return list(dict.fromkeys(out))


_ERJ_SIZE = {"0402": "2", "0603": "3", "0805": "6", "1206": "8"}
_KOA_SIZE = {"0402": "1E", "0603": "1J", "0805": "2A", "1206": "2B"}
_KEMET_VOLT = {Decimal("6.3"): "9", Decimal(10): "8", Decimal(16): "4", Decimal(25): "3", Decimal(50): "5",
               Decimal(100): "1"}
_KEMET_DIEL = {"X7R": "R", "X5R": "P", "C0G": "G"}
_YAGEO_VOLT = {Decimal("6.3"): "5", Decimal(10): "6", Decimal(16): "7", Decimal(25): "8", Decimal(50): "9",
               Decimal(100): "0"}


def _erj_1pct(pkg: str, eia4: str) -> str:
    if pkg == "0402":
        return f"ERJ-2RKF{eia4}X"
    if pkg == "0603":
        return f"ERJ-3EKF{eia4}V"
    return f"ERJ-{_ERJ_SIZE[pkg]}ENF{eia4}V"


def _erj_5pct(pkg: str, eia3: str) -> str:
    if pkg == "0402":
        return f"ERJ-2GEJ{eia3}X"
    return f"ERJ-{_ERJ_SIZE[pkg]}GEYJ{eia3}V"


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def search_keywords(spec: PassiveSpec) -> list[str]:
    """Palabras clave para la búsqueda en Mouser (la primera es la principal)."""
    if spec.value is None or spec.package is None:
        return []
    if spec.kind == RESISTOR:
        ohms = spec.value
        if ohms == 0:
            return [f"0 ohm jumper {spec.package}"]
        if ohms >= 1_000_000:
            text = f"{_plain(ohms / 1_000_000)}M"
        elif ohms >= 1000:
            text = f"{_plain(ohms / 1000)}K"
        else:
            text = _plain(ohms)
        return [f"{text} ohm {spec.package} resistor", f"{text} {spec.package}"]
    pf = spec.value * Decimal("1e12")
    words = []
    if pf < 10_000:
        words.append(f"{_plain(pf)}pF {spec.package} MLCC")
        if pf >= 1000:
            words.append(f"{_plain(pf / 1000)}nF {spec.package} MLCC")
    else:
        words.append(f"{_plain(pf / 1_000_000)}uF {spec.package} MLCC")
        if pf < 1_000_000:
            words.append(f"{_plain(pf / 1000)}nF {spec.package} MLCC")
    return words
