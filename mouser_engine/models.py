"""Estructuras de datos: partes de Mouser, líneas de BOM y resultados de cotización."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .utils import normalize_header, normalize_pn, parse_int, parse_lead_time_days, parse_number

# Niveles de estado, de menor a mayor gravedad.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"
LEVEL_EXCLUDED = "excluded"
LEVEL_PENDING = "pending"
LEVEL_ORDER = {LEVEL_EXCLUDED: 0, LEVEL_PENDING: 1, LEVEL_OK: 2, LEVEL_WARN: 3, LEVEL_ERROR: 4}


@dataclass(frozen=True)
class PriceBreak:
    quantity: int
    price: Decimal
    currency: str = ""
    text: str = ""


@dataclass(frozen=True)
class OnOrder:
    quantity: int
    date: str = ""


def lifecycle_level(status: str, discontinued: bool = False) -> str:
    """Clasifica el estado de ciclo de vida informado por Mouser."""
    if discontinued:
        return LEVEL_ERROR
    s = normalize_header(status)
    if not s:
        return LEVEL_OK
    if "obsolete" in s or "obsoleto" in s or "discontinued" in s:
        return LEVEL_ERROR
    if ("end of life" in s or s == "eol" or "last time buy" in s or "nrnd" in s
            or "not recommended" in s):
        return LEVEL_WARN
    return LEVEL_OK


def lifecycle_label(status: str, discontinued: bool = False) -> str:
    """Traducción corta del ciclo de vida para mostrar en pantalla."""
    if discontinued:
        return "Descontinuado"
    s = normalize_header(status)
    if not s:
        return "Activo"
    if "obsolete" in s:
        return "Obsoleto"
    if "discontinued" in s:
        return "Descontinuado"
    if "not recommended" in s or "nrnd" in s:
        return "No recomendado (NRND)"
    if "end of life" in s or s == "eol":
        return "Fin de vida (EOL)"
    if "last time buy" in s:
        return "Última compra (LTB)"
    if "new" in s:
        return "Nuevo"
    return status


_PACKAGING_ES = {
    "cut tape": "Cinta cortada",
    "reel": "Carrete",
    "full reel": "Carrete",
    "tray": "Bandeja",
    "tube": "Tubo",
    "bulk": "Granel",
    "bag": "Bolsa",
    "box": "Caja",
}


def packaging_label(text: str) -> str:
    """"Reel, Cut Tape, MouseReel" -> "Carrete, Cinta cortada, MouseReel"."""
    if not text:
        return ""
    return ", ".join(_PACKAGING_ES.get(p.strip().lower(), p.strip()) for p in text.split(","))


def lead_time_label(text: str) -> str:
    """"6 Weeks" -> "6 semanas", "56 Days" -> "56 días"."""
    if not text:
        return ""
    replacements = (("weeks", "semanas"), ("week", "semana"), ("days", "días"), ("day", "día"),
                    ("months", "meses"), ("month", "mes"))
    result = text
    for en, es in replacements:
        idx = result.lower().find(en)
        if idx >= 0:
            result = result[:idx] + es + result[idx + len(en):]
            break
    return result


def _breaks_with(raw_breaks: list, decimal: str | None) -> list[PriceBreak]:
    breaks: list[PriceBreak] = []
    for pb in raw_breaks:
        if not isinstance(pb, dict):
            continue
        qty = parse_int(pb.get("Quantity"))
        currency = str(pb.get("Currency") or "").strip()
        price = parse_number(pb.get("Price"), currency, decimal=decimal)
        if qty and qty > 0 and price is not None:
            breaks.append(PriceBreak(qty, price, currency, str(pb.get("Price") or "")))
    breaks.sort(key=lambda b: b.quantity)
    return breaks


def _non_increasing(breaks: list[PriceBreak]) -> bool:
    return all(later.price <= earlier.price for earlier, later in zip(breaks, breaks[1:]))


def parse_price_breaks(raw_breaks: list) -> list[PriceBreak]:
    """Tramos de precio de la Search API, leídos con la convención numérica de su moneda.

    Mouser formatea los precios con la convención local de la cuenta ("$1.234" en pesos chilenos,
    "$1,234.00" en dólares). Si con esa convención un tramo mayor quedara más caro que uno menor
    (imposible en una lista de precios), se prueba la otra convención y se usa la que da tramos
    coherentes.
    """
    breaks = _breaks_with(raw_breaks, None)
    if _non_increasing(breaks):
        return breaks
    for decimal in (",", "."):
        other = _breaks_with(raw_breaks, decimal)
        if len(other) == len(breaks) and _non_increasing(other):
            return other
    return breaks


@dataclass
class Part:
    """Un producto de Mouser tal como lo devuelve la Search API."""

    mouser_pn: str
    mpn: str
    manufacturer: str
    description: str = ""
    stock: int | None = None
    availability: str = ""
    factory_stock: int | None = None
    lead_time: str = ""
    lead_time_days: int | None = None
    lifecycle: str = ""
    discontinued: bool = False
    rohs: str = ""
    min_qty: int = 1
    mult: int = 1
    price_breaks: list[PriceBreak] = field(default_factory=list)
    on_order: list[OnOrder] = field(default_factory=list)
    product_url: str = ""
    datasheet_url: str = ""
    image_url: str = ""
    category: str = ""
    packaging: str = ""
    suggested_replacement: str = ""
    alternate_packagings: list[str] = field(default_factory=list)
    info_messages: list[str] = field(default_factory=list)
    restriction: str = ""
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        return normalize_pn(self.mouser_pn) or f"{normalize_pn(self.mpn)}@{normalize_header(self.manufacturer)}"

    @property
    def currency(self) -> str:
        for pb in self.price_breaks:
            if pb.currency:
                return pb.currency
        return ""

    @property
    def lifecycle_level(self) -> str:
        return lifecycle_level(self.lifecycle, self.discontinued)

    @property
    def lifecycle_label(self) -> str:
        return lifecycle_label(self.lifecycle, self.discontinued)

    @property
    def orderable(self) -> bool:
        return bool(self.price_breaks) and bool(self.mouser_pn) and self.mouser_pn.upper() != "N/A"

    @classmethod
    def from_api(cls, raw: dict) -> "Part":
        def text(name: str) -> str:
            value = raw.get(name)
            return "" if value is None else str(value).strip()

        breaks = parse_price_breaks(raw.get("PriceBreaks") or [])

        availability = text("Availability")
        stock = parse_int(raw.get("AvailabilityInStock"))
        if stock is None and availability:
            lowered = availability.lower()
            if "stock" in lowered and any(ch.isdigit() for ch in availability):
                stock = parse_int(availability)
            elif any(word in lowered for word in ("none", "non-stocked", "on order", "no stock")):
                stock = 0

        on_order: list[OnOrder] = []
        for entry in raw.get("AvailabilityOnOrder") or []:
            qty = parse_int(entry.get("Quantity"))
            if qty:
                on_order.append(OnOrder(qty, str(entry.get("Date") or "")[:10]))

        packaging_values: list[str] = []
        for attr in raw.get("ProductAttributes") or []:
            if str(attr.get("AttributeName", "")).strip().lower() == "packaging":
                value = str(attr.get("AttributeValue") or "").strip()
                if value and value not in packaging_values:
                    packaging_values.append(value)

        alternates = []
        for alt in raw.get("AlternatePackagings") or []:
            mpn = str(alt.get("APMfrPN") or "").strip() if isinstance(alt, dict) else str(alt).strip()
            if mpn:
                alternates.append(mpn)

        messages = [str(m).strip() for m in (raw.get("InfoMessages") or []) if str(m).strip()]
        for surcharge in raw.get("SurchargeMessages") or []:
            if isinstance(surcharge, dict) and surcharge.get("message"):
                messages.append(str(surcharge["message"]).strip())

        lead_time = text("LeadTime")
        discontinued = text("IsDiscontinued").lower() in ("true", "yes", "1")

        return cls(
            mouser_pn=text("MouserPartNumber"),
            mpn=text("ManufacturerPartNumber"),
            manufacturer=text("Manufacturer"),
            description=text("Description"),
            stock=stock,
            availability=availability,
            factory_stock=parse_int(raw.get("FactoryStock")),
            lead_time=lead_time,
            lead_time_days=parse_lead_time_days(lead_time),
            lifecycle=text("LifecycleStatus"),
            discontinued=discontinued,
            rohs=text("ROHSStatus"),
            min_qty=max(1, parse_int(raw.get("Min"), 1) or 1),
            mult=max(1, parse_int(raw.get("Mult"), 1) or 1),
            price_breaks=breaks,
            on_order=on_order,
            product_url=text("ProductDetailUrl"),
            datasheet_url=text("DataSheetUrl"),
            image_url=text("ImagePath"),
            category=text("Category"),
            packaging=", ".join(packaging_values),
            suggested_replacement=text("SuggestedReplacement"),
            alternate_packagings=alternates,
            info_messages=messages,
            restriction=text("RestrictionMessage"),
            raw=raw,
        )


@dataclass
class BomLine:
    """Una fila del archivo BOM."""

    row: int
    designators: str = ""
    mpn: str = ""
    manufacturer: str = ""
    mouser_pn: str = ""
    description: str = ""
    value: str = ""
    footprint: str = ""
    qty: int = 0
    raw: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)  # columnas con datos técnicos (tolerancia, tensión…)


@dataclass(eq=False)
class BomItem:
    """Parte a comprar: agrupa las líneas del BOM que usan el mismo número de parte.

    Se compara por identidad (eq=False): dos líneas distintas nunca son "la misma" parte a comprar.
    """

    id: int
    rows: list[int]
    designators: str = ""
    mpn: str = ""
    manufacturer: str = ""
    mouser_pn: str = ""
    description: str = ""
    value: str = ""
    footprint: str = ""
    qty_per_board: int = 0
    is_passive: bool = False
    include: bool = True
    lookup_state: str = "pending"  # pending | done | error | noquery
    lookup_error: str = ""
    candidates: list[Part] = field(default_factory=list)
    near: list[Part] = field(default_factory=list)
    suggestions: list[Part] = field(default_factory=list)
    manual_part: Part | None = None
    looked_up_at: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)
    spec: object | None = None  # passives.PassiveSpec para resistencias/condensadores sin MPN
    spec_report: dict | None = None  # resumen de la búsqueda por especificación

    @property
    def by_spec(self) -> bool:
        """Se cotiza por especificación (sin MPN ni código Mouser)."""
        return self.spec is not None and not (self.mpn or self.mouser_pn)

    @property
    def base_query(self) -> str:
        return (self.mouser_pn or self.mpn).strip()

    @property
    def rows_label(self) -> str:
        return ", ".join(str(r) for r in self.rows)

    @property
    def display_description(self) -> str:
        return self.description or self.value

    def options(self) -> list[Part]:
        """Todas las opciones conocidas en Mouser, sin duplicados."""
        seen: set[str] = set()
        result: list[Part] = []
        for part in ([self.manual_part] if self.manual_part else []) + self.candidates + self.near + self.suggestions:
            if part.key not in seen:
                seen.add(part.key)
                result.append(part)
        return result


@dataclass
class QuoteParams:
    boards: int = 1
    spares_pct: float = 0.0
    passive_spares_pct: float = 0.0
    optimize_breaks: bool = False
    freight: float = 0.0
    duty_pct: float = 0.0
    vat_pct: float = 0.0
    fx_rate: float = 0.0  # pesos chilenos por unidad de la moneda de Mouser (0 = no convertir)


@dataclass
class ItemQuote:
    """Resultado de cotizar un BomItem con ciertos parámetros.

    buy_qty / unit_price / ext_price son los valores finales (los que van al carro y al
    total). base_* es la compra mínima que cubre lo requerido y opt_* la cantidad que
    minimiza el costo usando los tramos de precio.
    """

    required: int = 0
    part: Part | None = None
    auto_selected: bool = True
    buy_qty: int = 0
    unit_price: Decimal | None = None
    ext_price: Decimal | None = None
    active_break: PriceBreak | None = None
    base_qty: int = 0
    base_unit_price: Decimal | None = None
    base_ext_price: Decimal | None = None
    opt_qty: int = 0
    opt_unit_price: Decimal | None = None
    opt_ext_price: Decimal | None = None
    savings: Decimal = Decimal(0)
    stock: int | None = None
    level: str = LEVEL_PENDING
    status: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def currency(self) -> str:
        return self.part.currency if self.part else ""

    def add(self, level: str, status: str, note: str | None = None) -> None:
        """Agrega una observación y eleva el estado si es más grave."""
        if note:
            self.notes.append(note)
        if LEVEL_ORDER.get(level, 0) > LEVEL_ORDER.get(self.level, 0) or not self.status:
            self.level = level
            self.status = status


@dataclass
class QuoteSummary:
    currency: str = ""
    items: int = 0
    included: int = 0
    ok: int = 0
    warn: int = 0
    error: int = 0
    excluded: int = 0
    pending: int = 0
    priced: int = 0
    unpriced: int = 0
    subtotal: Decimal = Decimal(0)
    optimized_subtotal: Decimal = Decimal(0)
    savings: Decimal = Decimal(0)
    goods: Decimal = Decimal(0)  # subtotal usado para costos (optimizado o no)
    freight: Decimal = Decimal(0)
    duty: Decimal = Decimal(0)
    vat: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
    total_clp: Decimal | None = None
    mixed_currency: bool = False
