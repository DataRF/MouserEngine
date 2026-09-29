import io
import json
import urllib.error
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from mouser_engine.landed import (
    RATES_URL,
    RULES_URL,
    ExchangeRates,
    ImportDataError,
    ImportRules,
    ImportSetup,
    Tier,
    builtin_rules,
    fetch_rates,
    fetch_rules,
    landed_cost,
    month_name,
    newest_rules,
    update_import_data,
)

RULES = ImportRules(
    version="2026-01-01", vat_pct=Decimal(19), duty_pct=Decimal(6), insurance_pct=Decimal(2), freight_in_cif=True,
    freight=(Tier(None, Decimal(59)),),
    brokerage=(Tier(Decimal(100), Decimal(20)), Tier(Decimal(1000), Decimal(50)), Tier(None, Decimal(96))),
    fallback_rate=Decimal(900))
RATES = ExchangeRates(usd=Decimal(950), usd_date="2026-09-29", customs=Decimal(940), customs_date="2026-08-28",
                      customs_month="2026-09")
SETUP = ImportSetup(RULES, RATES)


def test_builtin_rules_are_the_chilean_ones():
    rules = builtin_rules()
    assert (rules.vat_pct, rules.duty_pct, rules.insurance_pct) == (19, 6, 2)
    assert rules.freight and rules.brokerage and rules.source == "incluidas"
    assert date.fromisoformat(rules.version)
    again = ImportRules.from_json(rules.to_json(), "incluidas")
    assert again == rules


@pytest.mark.parametrize("change", [
    {"iva_pct": None}, {"iva_pct": -1}, {"derechos_pct": "seis"}, {"seguro_pct": True}, {"version": "ayer"},
    {"honorario_desaduanamiento_usd": []},
    {"honorario_desaduanamiento_usd": [{"hasta_cif_usd": 500, "usd": 50}, {"hasta_cif_usd": 100, "usd": 20}]},
    {"honorario_desaduanamiento_usd": [{"hasta_cif_usd": None, "usd": 50}, {"hasta_cif_usd": 100, "usd": 20}]},
    {"flete_mouser_usd": [{"hasta_fob_usd": None, "usd": 1e9}]},
    {"dolar_referencia": 0},
])
def test_invalid_rules_are_rejected(change):
    data = {**RULES.to_json(), **change}
    with pytest.raises(ImportDataError):
        ImportRules.from_json(data)
    with pytest.raises(ImportDataError):
        ImportRules.from_json(["no es un objeto"])


def test_tiers():
    assert [RULES.brokerage_usd(Decimal(v)) for v in ("50", "100", "100.01", "1000", "5000")] == [20, 20, 50, 50, 96]
    bounded = replace(RULES, brokerage=(Tier(Decimal(100), Decimal(20)), Tier(Decimal(1000), Decimal(50))))
    assert bounded.brokerage_usd(Decimal(5000)) == 50  # sobre el último límite rige el último tramo


def test_landed_cost_usd_account():
    cost = landed_cost(Decimal("1000.00"), "USD", SETUP)
    assert (cost.fob_usd, cost.insurance_usd, cost.declared_freight_usd, cost.cif_usd) == (1000, 20, 59, 1079)
    assert (cost.duty, cost.vat) == (Decimal("64.74"), Decimal("217.31"))  # 6 % del CIF; 19 % de CIF + derechos
    assert (cost.brokerage, cost.brokerage_vat) == (96, Decimal("18.24"))  # tramo sobre USD 1.000
    assert cost.freight == 59
    assert cost.total == Decimal("1455.29") == cost.goods + cost.freight + cost.import_total
    # Mouser al dólar observado; lo de DHL, línea por línea al dólar aduanero (como su factura)
    assert cost.total_clp == 1_006_050 + 60_856 + 204_271 + 90_240 + 17_146
    assert cost.vat_total == Decimal("235.55") and cost.net_total == cost.total - cost.vat_total


def test_landed_cost_clp_account():
    cost = landed_cost(Decimal(950_000), "CLP", SETUP)
    assert cost.fob_usd == 1000  # 950.000 pesos al dólar observado
    assert cost.freight == 56_050  # USD 59 al dólar observado
    assert (cost.duty, cost.vat, cost.brokerage, cost.brokerage_vat) == (60_856, 204_271, 90_240, 17_146)
    assert cost.total == cost.total_clp == 1_378_563
    assert cost.mouser_total == 1_006_050 and cost.import_total == 372_513


def test_freight_outside_cif_and_small_orders():
    rules = replace(RULES, freight_in_cif=False)
    cost = landed_cost(Decimal("50.00"), "USD", ImportSetup(rules, RATES))
    assert cost.declared_freight_usd == 0 and cost.cif_usd == Decimal("51.00")
    assert (cost.duty, cost.vat, cost.brokerage) == (Decimal("3.06"), Decimal("10.27"), 20)
    assert cost.total == Decimal("146.13")


def test_landed_cost_needs_something_to_buy_in_usd_or_clp():
    assert landed_cost(Decimal(0), "USD", SETUP) is None
    assert landed_cost(Decimal(100), "EUR", SETUP) is None
    assert landed_cost(Decimal(100), "", SETUP) is None


def test_rates_fallbacks():
    only_customs = ImportSetup(RULES, ExchangeRates(customs=Decimal(930)))
    assert only_customs.usd_rate == only_customs.customs_rate == 930
    nothing = ImportSetup(RULES)
    assert nothing.estimated_rates and nothing.usd_rate == nothing.customs_rate == 900
    assert not SETUP.estimated_rates and (SETUP.usd_rate, SETUP.customs_rate) == (950, 940)


def test_setup_json_roundtrip():
    again = ImportSetup.from_json(json.loads(json.dumps(SETUP.to_json())))
    assert again.rates == RATES and again.rules == replace(RULES, source="guardadas")
    assert ImportSetup.from_json({"rules": {"iva_pct": 19}}) is None
    assert ImportSetup.from_json("x") is None
    assert ExchangeRates.from_json({"usd": "abc", "customs": -3}) == ExchangeRates()


def test_newest_rules():
    newer = replace(RULES, version="2026-10-01", source="descargadas")
    assert newest_rules(RULES, newer) is newer
    assert newest_rules(newer, RULES) is newer
    same = replace(RULES, source="guardadas")
    assert newest_rules(RULES, same) is RULES
    assert newest_rules(None, RULES) is RULES


def test_month_name():
    assert month_name("2026-09") == "septiembre" and month_name("2027-01") == "enero"


# --- Descargas (sin red) -------------------------------------------------------------------

class FakeWeb:
    def __init__(self, pages: dict[str, object]):
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        page = self.pages.get(request.full_url)
        if page is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        if isinstance(page, Exception):
            raise page
        return io.BytesIO(json.dumps(page).encode("utf-8"))


def serie(*entries):
    return {"codigo": "dolar", "serie": [{"fecha": f"{day}T03:00:00.000Z", "valor": value}
                                         for day, value in reversed(entries)]}


def test_fetch_rates_takes_today_and_the_customs_dollar():
    web = FakeWeb({RATES_URL.format(year=2026): serie(
        ("2026-08-26", 931.2), ("2026-08-27", 932.4), ("2026-08-28", 933.9), ("2026-08-31", 936.1),
        ("2026-09-28", 941.5), ("2026-09-29", 942.25), ("2026-09-30", 945.0))})
    rates = fetch_rates(date(2026, 9, 29), opener=web)
    assert (rates.usd, rates.usd_date) == (Decimal("942.25"), "2026-09-29")  # no el de mañana
    # dólar aduanero de septiembre: observado del penúltimo día hábil de agosto
    assert (rates.customs, rates.customs_date, rates.customs_month) == (Decimal("933.9"), "2026-08-28", "2026-09")
    assert rates.fetched_at and web.urls == [RATES_URL.format(year=2026)]


def test_fetch_rates_in_january_reads_last_year():
    web = FakeWeb({RATES_URL.format(year=2027): serie(("2027-01-04", 950.0)),
                   RATES_URL.format(year=2026): serie(("2026-12-29", 948.0), ("2026-12-30", 949.5),
                                                      ("2026-12-31", 951.0))})
    rates = fetch_rates(date(2027, 1, 5), opener=web)
    assert rates.usd == 950 and (rates.customs, rates.customs_date) == (Decimal("949.5"), "2026-12-30")


def test_fetch_rates_errors():
    with pytest.raises(ImportDataError, match="mindicador.cl"):
        fetch_rates(date(2026, 9, 29), opener=FakeWeb({}))
    with pytest.raises(ImportDataError, match="dólar observado"):
        fetch_rates(date(2026, 9, 29), opener=FakeWeb({RATES_URL.format(year=2026): {"serie": []}}))
    offline = FakeWeb({RATES_URL.format(year=2026): urllib.error.URLError("sin red")})
    with pytest.raises(ImportDataError, match="sin red"):
        fetch_rates(date(2026, 9, 29), opener=offline)


def test_fetch_rules_and_update():
    newer = {**RULES.to_json(), "version": "2026-12-01"}
    web = FakeWeb({RULES_URL: newer, RATES_URL.format(year=2026): serie(("2026-08-28", 933.9),
                                                                        ("2026-08-31", 936.1),
                                                                        ("2026-09-29", 942.25))})
    rules = fetch_rules(opener=web)
    assert rules.version == "2026-12-01" and rules.source == "descargadas"
    update = update_import_data(date(2026, 9, 29), opener=web)
    assert update.rates.usd == Decimal("942.25") and update.rules.version == "2026-12-01"
    assert not update.rates_error and not update.rules_error
    broken = update_import_data(date(2026, 9, 29), opener=FakeWeb({RULES_URL: {"version": "x"}}))
    assert broken.rates is None and "mindicador.cl" in broken.rates_error
    assert broken.rules is None and "inválidas" in broken.rules_error
