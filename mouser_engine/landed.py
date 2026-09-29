"""Precio con todo incluido: lo que cuesta la compra de Mouser puesta en Chile.

La compra llega por DHL Express (courier). Además de la mercancía y el flete que cobra Mouser, al
entregar DHL cobra los impuestos de importación y su honorario de desaduanamiento:

    FOB        valor de la mercancía en dólares
    Seguro     2 % del FOB (seguro presunto: el envío no trae póliza)
    CIF        FOB + flete + seguro
    Derechos   6 % del CIF
    IVA        19 % de (CIF + derechos)
    Honorario  de desaduanamiento de DHL, según el tramo del CIF, más 19 % de IVA

DHL cobra en pesos al dólar aduanero del mes: el dólar observado del penúltimo día hábil bancario
del mes anterior. Los tipos de cambio salen de mindicador.cl (Banco Central de Chile) y las reglas
(porcentajes, flete de Mouser y tramos de DHL) de assets/importacion_cl.json, que la aplicación
también descarga del repositorio al abrirse para tenerlas al día sin reinstalar.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import __version__

RULES_FILE = Path(__file__).resolve().parent / "assets" / "importacion_cl.json"
RULES_URL = "https://raw.githubusercontent.com/DataRF/MouserEngine/main/mouser_engine/assets/importacion_cl.json"
RATES_URL = "https://mindicador.cl/api/dolar/{year}"
CURRENCIES = ("USD", "CLP")  # monedas de cuenta de Mouser para las que se calcula

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)


class ImportDataError(Exception):
    """No se pudieron obtener o validar los tipos de cambio o las reglas de importación."""


def _usd(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _pesos(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def month_name(month: str) -> str:
    """"2026-09" -> "septiembre"."""
    try:
        return MONTHS[int(month[5:7]) - 1]
    except (ValueError, IndexError):
        return month


# --- Reglas ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Tier:
    up_to: Decimal | None  # límite superior del tramo en USD (None = sin límite)
    amount: Decimal  # USD


def _tier_amount(tiers: tuple[Tier, ...], value: Decimal) -> Decimal:
    for tier in tiers:
        if tier.up_to is None or value <= tier.up_to:
            return tier.amount
    return tiers[-1].amount  # sobre el último límite rige el último tramo


def _number(value: object, low: float, high: float) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"valor no numérico: {value!r}")
    number = Decimal(str(value))
    if not number.is_finite() or not Decimal(str(low)) <= number <= Decimal(str(high)):
        raise ValueError(f"valor fuera de rango: {value!r}")
    return number


def _tiers(raw: object, limit_key: str) -> tuple[Tier, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("faltan los tramos")
    tiers: list[Tier] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("tramo inválido")
        if tiers and tiers[-1].up_to is None:
            raise ValueError("hay tramos después del tramo sin límite")
        limit = entry.get(limit_key)
        up_to = None if limit is None else _number(limit, 0.01, 1e9)
        if up_to is not None and tiers and up_to <= tiers[-1].up_to:
            raise ValueError("los tramos deben ir de menor a mayor")
        tiers.append(Tier(up_to, _number(entry.get("usd"), 0, 100_000)))
    return tuple(tiers)


def _tiers_json(tiers: tuple[Tier, ...], limit_key: str) -> list[dict]:
    return [{limit_key: float(t.up_to) if t.up_to is not None else None, "usd": float(t.amount)} for t in tiers]


@dataclass(frozen=True)
class ImportRules:
    version: str  # AAAA-MM-DD
    vat_pct: Decimal
    duty_pct: Decimal
    insurance_pct: Decimal
    freight_in_cif: bool
    freight: tuple[Tier, ...]  # flete que cobra Mouser, según el FOB
    brokerage: tuple[Tier, ...]  # honorario de desaduanamiento de DHL, según el CIF
    fallback_rate: Decimal  # pesos por dólar si nunca hubo conexión para obtener el del día
    notes: tuple[str, ...] = ()
    source: str = "incluidas"  # incluidas en el programa | descargadas | guardadas

    def freight_usd(self, fob_usd: Decimal) -> Decimal:
        return _tier_amount(self.freight, fob_usd)

    def brokerage_usd(self, cif_usd: Decimal) -> Decimal:
        return _tier_amount(self.brokerage, cif_usd)

    @classmethod
    def from_json(cls, data: object, source: str = "incluidas") -> "ImportRules":
        try:
            if not isinstance(data, dict):
                raise ValueError("se esperaba un objeto JSON")
            version = str(data.get("version") or "")
            date.fromisoformat(version)
            notes = data.get("notas") or []
            return cls(
                version=version,
                vat_pct=_number(data.get("iva_pct"), 0, 100),
                duty_pct=_number(data.get("derechos_pct"), 0, 100),
                insurance_pct=_number(data.get("seguro_pct"), 0, 100),
                freight_in_cif=bool(data.get("flete_en_cif", True)),
                freight=_tiers(data.get("flete_mouser_usd"), "hasta_fob_usd"),
                brokerage=_tiers(data.get("honorario_desaduanamiento_usd"), "hasta_cif_usd"),
                fallback_rate=_number(data.get("dolar_referencia"), 100, 100_000),
                notes=tuple(str(n) for n in notes if isinstance(n, str))[:20],
                source=source,
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ImportDataError(f"Reglas de importación inválidas: {exc}") from exc

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "iva_pct": float(self.vat_pct),
            "derechos_pct": float(self.duty_pct),
            "seguro_pct": float(self.insurance_pct),
            "flete_en_cif": self.freight_in_cif,
            "flete_mouser_usd": _tiers_json(self.freight, "hasta_fob_usd"),
            "honorario_desaduanamiento_usd": _tiers_json(self.brokerage, "hasta_cif_usd"),
            "dolar_referencia": float(self.fallback_rate),
            "notas": list(self.notes),
        }


def builtin_rules() -> ImportRules:
    """Las reglas incluidas en el programa."""
    return ImportRules.from_json(json.loads(RULES_FILE.read_text(encoding="utf-8")), "incluidas")


def newest_rules(*candidates: ImportRules | None) -> ImportRules:
    """Las reglas más recientes (a igual versión, la primera de la lista)."""
    valid = [c for c in candidates if c is not None]
    best = valid[0]
    for rules in valid[1:]:
        if rules.version > best.version:
            best = rules
    return best


# --- Tipos de cambio -------------------------------------------------------------------------

def _decimal_or_none(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


@dataclass(frozen=True)
class ExchangeRates:
    usd: Decimal | None = None  # dólar observado: pesos por dólar
    usd_date: str = ""  # AAAA-MM-DD
    customs: Decimal | None = None  # dólar aduanero del mes
    customs_date: str = ""  # día del dólar observado que rige como aduanero
    customs_month: str = ""  # AAAA-MM en que rige
    fetched_at: str = ""  # AAAA-MM-DDTHH:MM:SS

    @property
    def available(self) -> bool:
        return bool(self.usd or self.customs)

    def fetched_on(self, day: date) -> bool:
        return self.fetched_at[:10] == day.isoformat()

    def to_json(self) -> dict:
        return {"usd": str(self.usd) if self.usd else None, "usd_date": self.usd_date,
                "customs": str(self.customs) if self.customs else None, "customs_date": self.customs_date,
                "customs_month": self.customs_month, "fetched_at": self.fetched_at}

    @classmethod
    def from_json(cls, data: object) -> "ExchangeRates":
        if not isinstance(data, dict):
            return cls()
        return cls(_decimal_or_none(data.get("usd")), str(data.get("usd_date") or ""),
                   _decimal_or_none(data.get("customs")), str(data.get("customs_date") or ""),
                   str(data.get("customs_month") or ""), str(data.get("fetched_at") or ""))


Opener = Callable[..., object]


def _get_json(url: str, opener: Opener | None, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                  "User-Agent": f"MouserEngine/{__version__}"})
    try:
        response = (opener or urllib.request.urlopen)(request, timeout=timeout)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - cualquier falla de red o de formato se informa igual
        raise ImportDataError(f"No se pudo consultar {urlparse(url).netloc}: {exc}") from exc


def _series(data: object) -> dict[date, Decimal]:
    values: dict[date, Decimal] = {}
    for entry in (data.get("serie") if isinstance(data, dict) else None) or []:
        try:
            day = date.fromisoformat(str(entry["fecha"])[:10])  # la fecha es la medianoche de Chile en UTC
        except (KeyError, TypeError, ValueError):
            continue
        value = _decimal_or_none(entry.get("valor"))
        if value is not None:
            values[day] = value
    return values


def fetch_rates(today: date | None = None, opener: Opener | None = None, timeout: float = 10.0) -> ExchangeRates:
    """Dólar observado más reciente y dólar aduanero del mes (Banco Central de Chile, vía mindicador.cl)."""
    today = today or date.today()
    previous = today.replace(day=1) - timedelta(days=1)  # último día del mes anterior
    values = _series(_get_json(RATES_URL.format(year=today.year), opener, timeout))
    if previous.year != today.year:
        values.update(_series(_get_json(RATES_URL.format(year=previous.year), opener, timeout)))
    current = [day for day in values if day <= today]
    if not current:
        raise ImportDataError("mindicador.cl no entregó el dólar observado.")
    latest = max(current)
    last_month = sorted(day for day in values if (day.year, day.month) == (previous.year, previous.month))
    customs_day = last_month[-2] if len(last_month) >= 2 else None  # penúltimo día hábil bancario
    return ExchangeRates(
        usd=values[latest], usd_date=latest.isoformat(),
        customs=values[customs_day] if customs_day else None,
        customs_date=customs_day.isoformat() if customs_day else "",
        customs_month=f"{today:%Y-%m}",
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )


def fetch_rules(opener: Opener | None = None, timeout: float = 10.0) -> ImportRules:
    """Las reglas vigentes publicadas en el repositorio (rama main)."""
    return ImportRules.from_json(_get_json(RULES_URL, opener, timeout), "descargadas")


@dataclass
class ImportUpdate:
    rates: ExchangeRates | None = None
    rules: ImportRules | None = None
    rates_error: str = ""  # lo que falló queda aquí y se sigue usando lo guardado
    rules_error: str = ""


def update_import_data(today: date | None = None, opener: Opener | None = None,
                       timeout: float = 10.0) -> ImportUpdate:
    """Tipos de cambio del día y reglas vigentes."""
    update = ImportUpdate()
    try:
        update.rates = fetch_rates(today, opener, timeout)
    except ImportDataError as exc:
        update.rates_error = str(exc)
    try:
        update.rules = fetch_rules(opener, timeout)
    except ImportDataError as exc:
        update.rules_error = str(exc)
    return update


# --- Cálculo ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportSetup:
    """Todo lo necesario para calcular el precio puesto en Chile."""

    rules: ImportRules
    rates: ExchangeRates = field(default_factory=ExchangeRates)

    @property
    def usd_rate(self) -> Decimal:
        """Pesos por dólar para lo que cobra Mouser (dólar observado)."""
        return self.rates.usd or self.rates.customs or self.rules.fallback_rate

    @property
    def customs_rate(self) -> Decimal:
        """Pesos por dólar para lo que cobra DHL (dólar aduanero del mes)."""
        return self.rates.customs or self.rates.usd or self.rules.fallback_rate

    @property
    def estimated_rates(self) -> bool:
        """True si no hay tipos de cambio reales y se usa el dólar de referencia."""
        return not self.rates.available

    def to_json(self) -> dict:
        return {"rules": self.rules.to_json(), "rates": self.rates.to_json()}

    @classmethod
    def from_json(cls, data: object) -> "ImportSetup | None":
        if not isinstance(data, dict):
            return None
        try:
            rules = ImportRules.from_json(data.get("rules"), "guardadas")
        except ImportDataError:
            return None
        return cls(rules, ExchangeRates.from_json(data.get("rates")))


@dataclass(frozen=True)
class LandedCost:
    """Desglose del precio puesto en Chile. Los montos sin sufijo están en la moneda de la cotización."""

    currency: str
    goods: Decimal  # mercancía (lo que cobra Mouser por las partes)
    freight: Decimal  # flete que cobra Mouser
    duty: Decimal  # derechos de aduana
    vat: Decimal  # IVA de la importación
    brokerage: Decimal  # honorario de desaduanamiento de DHL
    brokerage_vat: Decimal  # IVA del honorario
    total: Decimal
    total_clp: Decimal
    fob_usd: Decimal
    freight_usd: Decimal
    declared_freight_usd: Decimal  # flete que entra al CIF
    insurance_usd: Decimal
    cif_usd: Decimal
    duty_usd: Decimal
    vat_usd: Decimal
    brokerage_usd: Decimal
    brokerage_vat_usd: Decimal
    usd_rate: Decimal
    customs_rate: Decimal
    setup: ImportSetup

    @property
    def mouser_total(self) -> Decimal:
        return self.goods + self.freight

    @property
    def import_total(self) -> Decimal:
        """Lo que cobra DHL al entregar."""
        return self.duty + self.vat + self.brokerage + self.brokerage_vat

    @property
    def import_total_usd(self) -> Decimal:
        return self.duty_usd + self.vat_usd + self.brokerage_usd + self.brokerage_vat_usd

    @property
    def import_charges_clp(self) -> tuple[Decimal, ...]:
        """Derechos, IVA, honorario e IVA del honorario en pesos, línea por línea como los cobra DHL."""
        return tuple(_pesos(value * self.customs_rate)
                     for value in (self.duty_usd, self.vat_usd, self.brokerage_usd, self.brokerage_vat_usd))

    def to_clp(self, value: Decimal) -> Decimal:
        """Un monto en la moneda de la cotización, en pesos (lo que cobra Mouser va al dólar observado)."""
        return _pesos(value if self.currency == "CLP" else value * self.usd_rate)

    @property
    def vat_total(self) -> Decimal:
        """IVA incluido en el total (para una empresa es crédito fiscal)."""
        return self.vat + self.brokerage_vat

    @property
    def net_total(self) -> Decimal:
        return self.total - self.vat_total


def landed_cost(goods: Decimal, currency: str, setup: ImportSetup) -> LandedCost | None:
    """Precio puesto en Chile de una compra de `goods` (en la moneda de la cuenta de Mouser).

    None si no hay nada que comprar o la moneda no es USD ni CLP.
    """
    currency = (currency or "").upper()
    if currency not in CURRENCIES or goods is None or goods <= 0:
        return None
    rules = setup.rules
    usd_rate, customs_rate = setup.usd_rate, setup.customs_rate
    fob = _usd(goods if currency == "USD" else goods / usd_rate)
    freight_usd = _usd(rules.freight_usd(fob))
    insurance = _usd(fob * rules.insurance_pct / _HUNDRED)
    declared = freight_usd if rules.freight_in_cif else Decimal("0.00")
    cif = fob + declared + insurance
    duty = _usd(cif * rules.duty_pct / _HUNDRED)
    vat = _usd((cif + duty) * rules.vat_pct / _HUNDRED)
    brokerage = _usd(rules.brokerage_usd(cif))
    brokerage_vat = _usd(brokerage * rules.vat_pct / _HUNDRED)
    charges_usd = (duty, vat, brokerage, brokerage_vat)
    charges_clp = tuple(_pesos(value * customs_rate) for value in charges_usd)  # como la factura de DHL
    if currency == "USD":
        freight = freight_usd
        charges = charges_usd
        total = goods + freight + sum(charges_usd)
        total_clp = _pesos((goods + freight) * usd_rate) + sum(charges_clp)
    else:
        freight = _pesos(freight_usd * usd_rate)
        charges = charges_clp
        total = goods + freight + sum(charges_clp)
        total_clp = _pesos(total)
    return LandedCost(currency, goods, freight, *charges, total, total_clp, fob, freight_usd, declared, insurance,
                      cif, *charges_usd, usd_rate, customs_rate, setup)
