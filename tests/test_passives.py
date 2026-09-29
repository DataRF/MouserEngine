from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.lookup import lookup_all, resolve_specs
from mouser_engine.models import Part, QuoteParams
from mouser_engine.passives import (
    CAPACITOR,
    RESISTOR,
    Defaults,
    candidate_spec,
    constructed_candidates,
    eia_code,
    evaluate,
    format_capacitance,
    format_resistance,
    mpn_package,
    parse_capacitance,
    parse_dielectric,
    parse_package,
    parse_power,
    parse_resistance,
    parse_spec,
    parse_voltage,
    rkm_code,
    search_keywords,
)
from mouser_engine.quote import apply_lookup, queries_for, quote_all, specs_for

D = Decimal


@pytest.mark.parametrize("text,bare,expected", [
    ("10k", True, "10000"), ("10K", True, "10000"), ("10 kΩ", True, "10000"), ("4k7", True, "4700"),
    ("4K7", True, "4700"), ("4.7k", True, "4700"), ("4,7k", True, "4700"), ("100R", True, "100"),
    ("100", True, "100"), ("0R", True, "0"), ("0", True, "0"), ("1M", True, "1000000"),
    ("2M2", True, "2200000"), ("1R5", True, "1.5"), ("R47", True, "0.47"), ("10K0", True, "10000"),
    ("49R9", True, "49.9"), ("10kohm", True, "10000"), ("2.2 Mohm", True, "2200000"), ("100mΩ", True, "0.1"),
    ("10k 1% 0603", True, "10000"), ("Resistencia 10 kΩ", False, "10000"),
])
def test_parse_resistance(text, bare, expected):
    assert parse_resistance(text, bare_numbers=bare) == D(expected)


@pytest.mark.parametrize("text", ["1m", "0603", "Resistencia 0603", ""])
def test_parse_resistance_rejects_ambiguous(text):
    assert parse_resistance(text) is None


@pytest.mark.parametrize("text,expected", [
    ("100n", "100e-9"), ("100nF", "100e-9"), ("0.1uF", "0.1e-6"), ("0.1µF", "0.1e-6"), (".1uF", "0.1e-6"),
    ("4u7", "4.7e-6"), ("2n2", "2.2e-9"), ("10p", "10e-12"), ("1p5", "1.5e-12"), ("22uF/16V", "22e-6"),
    ("100 nF", "100e-9"), ("1000pF", "1e-9"), ("4,7uF", "4.7e-6"),
])
def test_parse_capacitance(text, expected):
    assert parse_capacitance(text) == D(expected)


@pytest.mark.parametrize("text", ["104", "0.1", "100", "X7R 16V"])
def test_parse_capacitance_requires_unit(text):
    assert parse_capacitance(text) is None


@pytest.mark.parametrize("text,expected", [
    ("R0603", "0603"), ("C_0603_1608Metric", "0603"), ("Resistor_SMD:R_0402_1005Metric", "0402"),
    ("RESC1608X55N", "0603"), ("CAPC2012X125N", "0805"), ("1206 (3216 metric)", "1206"),
    ("R_1206_3216Metric_Pad1.30x1.75mm", "1206"), ("C1206K", "1206"), ("CHIP-0603(1608-METRIC)", "0603"),
    ("1608Metric", "0603"), ("TO-92", None), ("RES 1608", None), ("SOIC-8", None),
])
def test_parse_package(text, expected):
    assert parse_package(text) == expected


def test_parse_small_specs():
    assert parse_power("1/10W") == D("0.1") and parse_power("100mW") == D("0.1")
    assert parse_power("1/4 Watt") == D("0.25") and parse_power("0,125 W") == D("0.125")
    assert parse_voltage("16V") == 16 and parse_voltage("50 VDC") == 50 and parse_voltage("6V3") == D("6.3")
    assert parse_voltage("16volts") == 16 and parse_voltage("1kV") == 1000 and parse_voltage("X7R") is None
    assert parse_dielectric("NP0") == "C0G" and parse_dielectric("COG 22pF") == "C0G"
    assert parse_dielectric("x7r") == "X7R" and parse_dielectric("sin dato") is None


@pytest.mark.parametrize("text,expected", [
    ("±1%", "1"), ("5 %", "5"), ("+/-10%", "10"), ("+80-20%", "80"), ("+80/-20%", "80"), ("+80%/-20%", "80"),
])
def test_parse_tolerance(text, expected):
    from mouser_engine.passives import parse_tolerance

    assert parse_tolerance(text) == D(expected)


def test_parse_spec_resistor_and_capacitor():
    spec = parse_spec("R1, R2", "10k", "Resistencia 1% 1/10W", "R0603")
    assert (spec.kind, spec.value, spec.package, spec.tolerance, spec.power) == (RESISTOR, 10000, "0603", 1, D("0.1"))
    assert spec.complete and spec.label() == "Resistencia 10 kΩ ±1 % 1/10 W 0603"
    cap = parse_spec("C4", "100n", "", "C_0603_1608Metric", {"Voltage": "25V", "Dielectric": "X7R"})
    assert (cap.kind, cap.value, cap.voltage, cap.dielectric, cap.package) == (CAPACITOR, D("1e-7"), 25, "X7R", "0603")
    assert cap.label() == "Condensador 100 nF 25 V X7R 0603"


def test_parse_spec_incomplete_and_excluded():
    assert parse_spec("R3", "10k", "", "").missing == ("encapsulado",)
    assert "capacidad" in parse_spec("C3", "104", "", "0603").missing[0]
    electrolytic = parse_spec("C5", "100uF", "Electrolítico 25V", "0805")
    assert not electrolytic.complete and "electrolítico" in electrolytic.missing[0]
    assert parse_spec("U1", "LM358", "Op amp", "SOIC-8") is None
    assert parse_spec("RV1", "10k", "Potenciómetro", "") is None
    assert parse_spec("L1", "10uH", "Inductor", "0805") is None


def test_parse_spec_kind_from_text_without_designators():
    assert parse_spec("", "4.7k", "Resistencia 0603 1%", "").kind == RESISTOR
    assert parse_spec("", "", "Condensador cerámico 1uF 0402 10V", "").kind == CAPACITOR


@pytest.mark.parametrize("mpn,manufacturer,expected", [
    ("RC0603FR-0710KL", "YAGEO", "0603"), ("CRCW060310K0FKEA", "Vishay", "0603"),
    ("ERJ-3EKF1002V", "Panasonic", "0603"), ("ERJ-2RKF1002X", "Panasonic", "0402"),
    ("RK73H1JTTD1002F", "KOA Speer", "0603"), ("MCR03EZPFX1002", "ROHM", "0603"),
    ("GRM188R71C104KA01D", "Murata", "0603"), ("GRM155R71C104KA88D", "Murata", "0402"),
    ("CL10B104KB8NNNC", "Samsung Electro-Mechanics", "0603"), ("RC1608F103CS", "Samsung Electro-Mechanics", "0603"),
    ("C0603C104K4RACTU", "KEMET", "0603"), ("C1608X7R1C104K080AA", "TDK", "0603"),
    ("C0603X5R0J104M030BC", "TDK", "0201"), ("CGA3E2X7R1C104K080AA", "TDK", "0603"),
    ("EMK107B7104KA-T", "Taiyo Yuden", "0603"), ("06035C104KAT2A", "KYOCERA AVX", "0603"),
    ("CC0603KRX7R9BB104", "YAGEO", "0603"), ("WR06X1002FTL", "Walsin", None), ("LM358DR", "TI", None),
])
def test_mpn_package(mpn, manufacturer, expected):
    assert mpn_package(mpn, manufacturer) == expected


def test_codes():
    assert [rkm_code(D(v)) for v in ("10000", "4700", "100", "1000000", "49.9", "1")] == \
        ["10K", "4K7", "100R", "1M", "49R9", "1R"]
    assert [rkm_code(D(v), 4) for v in ("10000", "4700", "100", "1000000")] == ["10K0", "4K70", "100R", "1M00"]
    assert [eia_code(D(v), 3) for v in ("10000", "4700", "100", "49.9", "1")] == ["1002", "4701", "1000", "49R9", "1R00"]
    assert [eia_code(D(v), 2) for v in ("10000", "4700", "100", "47")] == ["103", "472", "101", "470"]
    assert eia_code(D("49.9"), 2) is None
    assert format_resistance(D("4700")) == "4,7 kΩ" and format_capacitance(D("1e-7")) == "100 nF"


def test_constructed_candidates_are_real_part_numbers():
    ten_k = constructed_candidates(parse_spec("R1", "10k", "1%", "0603"))
    assert ten_k == ["RC0603FR-0710KL", "CRCW060310K0FKEA", "RMCF0603FT10K0", "ERJ-3EKF1002V",
                     "RK73H1JTTD1002F", "CR0603-FX-1002ELF"]
    five_pct = constructed_candidates(parse_spec("R1", "10k", "5%", "0603"))
    assert {"RC0603JR-0710KL", "CRCW060310K0JNEA", "ERJ-3GEYJ103V", "RK73B1JTTD103J", "CR0603-JW-103ELF"} <= set(five_pct)
    small = constructed_candidates(parse_spec("R1", "100", "1%", "0402"))
    assert {"CRCW0402100RFKED", "ERJ-2RKF1000X", "RK73H1ETTP1000F", "CR0402-FX-1000GLF"} <= set(small)
    caps = constructed_candidates(parse_spec("C1", "100nF", "16V X7R 10%", "0603"))
    assert {"C0603C104K4RACTU", "CC0603KRX7R7BB104", "CC0603KRX7R9BB104"} <= set(caps)
    c0g = constructed_candidates(parse_spec("C1", "22pF", "50V C0G 5%", "0603"))
    assert c0g[:2] == ["C0603C220J5GACTU", "CC0603JRNPO9BN220"]
    assert constructed_candidates(parse_spec("R1", "10k", "0.1%", "0603")) == []  # requiere película delgada


def test_search_keywords():
    assert search_keywords(parse_spec("R1", "4.7k", "", "0603")) == ["4.7K ohm 0603 resistor", "4.7K 0603"]
    assert search_keywords(parse_spec("C1", "100nF", "", "0603")) == ["0.1uF 0603 MLCC", "100nF 0603 MLCC"]
    assert search_keywords(parse_spec("C1", "1nF", "", "0402")) == ["1000pF 0402 MLCC", "1nF 0402 MLCC"]
    assert search_keywords(parse_spec("C1", "22pF", "", "0603")) == ["22pF 0603 MLCC"]


def mk(mpn, manufacturer, description, category, stock=10000, lifecycle="", price="0.01"):
    return Part.from_api({
        "MouserPartNumber": "X-" + mpn, "ManufacturerPartNumber": mpn, "Manufacturer": manufacturer,
        "Description": description, "Category": category, "AvailabilityInStock": str(stock),
        "LifecycleStatus": lifecycle, "Min": "1", "Mult": "1",
        "PriceBreaks": [{"Quantity": 1, "Price": "$" + price, "Currency": "USD"}]})


RES = "Thick Film Resistors - SMD"
CAP = "Multilayer Ceramic Capacitors MLCC - SMD/SMT"


@pytest.mark.parametrize("part,reason", [
    (mk("CRCW060310K0FKEA", "Vishay / Dale", RES + " 1/10watt 10Kohms 1% 100ppm", RES), ""),
    (mk("RT0603BRD0710KL", "YAGEO", "Thin Film Resistors - SMD 10K ohm 0.1% 25ppm", "Thin Film Resistors - SMD"), ""),
    (mk("RC0603JR-0710KL", "YAGEO", RES + " 10K ohm 5% 0603", RES), "tolerancia insuficiente"),
    (mk("RC0402FR-0710KL", "YAGEO", RES + " 10K ohm 1% 0402", RES), "encapsulado distinto"),
    (mk("RC0603FR-0722KL", "YAGEO", RES + " 22K ohm 1% 0603", RES), "valor distinto"),
    (mk("RC0603FR-0710KL", "YAGEO", RES + " 10K ohm 1% 0603", RES, lifecycle="Obsolete"), "obsoleta o no recomendada"),
    (mk("4610X-101-103LF", "Bourns", "Resistor Networks & Arrays 10K ohm 2%", "Resistor Networks & Arrays"),
     "otro tipo de componente"),
    (mk("XYZ10K", "Acme", RES + " 10K ohm 1%", RES), "encapsulado no verificable"),
])
def test_evaluate_resistor(part, reason):
    spec = parse_spec("R1", "10k", "1% 1/16W", "0603")
    result = evaluate(spec, part, Defaults())
    assert result.ok == (reason == "") and result.reason == reason


@pytest.mark.parametrize("part,reason", [
    (mk("GRM188R71C104KA01D", "Murata", CAP + " 0.1uF 16volts X7R 10%", CAP), ""),
    (mk("CL10B104KB8NNNC", "Samsung Electro-Mechanics", CAP + " 0603 0.1uF 50volts X7R 10%", CAP), ""),
    (mk("CL10F104ZB8NNNC", "Samsung Electro-Mechanics", CAP + " 0603 0.1uF 50volts Y5V +80-20%", CAP),
     "tolerancia insuficiente"),
    (mk("CL10A104KP8NNNC", "Samsung Electro-Mechanics", CAP + " 0603 0.1uF 10volts X5R 10%", CAP), "tensión insuficiente"),
    (mk("06035C104KAT2A", "KYOCERA AVX", CAP + " 50V .1uF X7R 0603 10%", CAP), ""),
])
def test_evaluate_capacitor_with_defaults(part, reason):
    spec = parse_spec("C1", "100nF", "", "0603")  # sin tensión, tolerancia ni dieléctrico
    result = evaluate(spec, part, Defaults(cap_voltage=16))
    assert result.ok == (reason == "") and result.reason == reason
    assert any("Tensión no indicada" in note for note in result.assumptions)


def test_evaluate_dielectric_rules():
    x5r_spec = parse_spec("C1", "1uF", "X5R 10V", "0402")
    x7r = mk("GRM155R71A105KE01D", "Murata", CAP + " 1uF 10volts X7R 10%", CAP)
    assert evaluate(x5r_spec, x7r, Defaults()).ok  # X7R supera a X5R
    x7r_spec = parse_spec("C1", "1uF", "X7R 10V", "0402")
    x5r = mk("GRM155R61A105KE15D", "Murata", CAP + " 1uF 10volts X5R 10%", CAP)
    assert evaluate(x7r_spec, x5r, Defaults()).reason == "dieléctrico inferior"
    c0g_spec = parse_spec("C1", "22pF", "C0G 50V 5%", "0603")
    c0g = mk("GRM1885C1H220JA01D", "Murata", CAP + " 22pF 50volts C0G 5%", CAP)
    assert evaluate(c0g_spec, c0g, Defaults()).ok


def test_candidate_spec_tolerance_in_pf():
    part = mk("GRM1885C1H1R0CA01D", "Murata", CAP + " 1pF 50volts C0G ±0.25pF", CAP)
    spec = candidate_spec(part)
    assert spec.value == D("1e-12") and spec.tolerance == D("25")


def test_resolve_specs_prefers_recognized_cheapest(client, server):
    spec = parse_spec("R11", "4.7k", "1%", "0603")
    results = resolve_specs(client, [spec], Defaults(), {spec.key: 11})
    result = results[spec.key]
    mpns = {p.mpn for p in result.candidates}
    assert {"RC0603FR-074K7L", "CRCW06034K70FKEA", "XR0603-4K7"} <= mpns
    assert "RC0603JR-074K7L" not in mpns
    assert result.rejected.get("tolerancia insuficiente") == 1
    assert result.searches == ["4.7K ohm 0603 resistor"]  # con 2 opciones reconocidas basta una búsqueda
    assert result.constructed == 0


def test_resolve_specs_uses_constructed_part_numbers_when_needed(client, server):
    spec = parse_spec("R1", "10k", "1%", "0603")
    results = resolve_specs(client, [spec], Defaults(), {spec.key: 10})
    result = results[spec.key]
    assert [p.mpn for p in result.candidates] == ["RC0603FR-0710KL"]
    assert result.constructed == 6
    exact_calls = [c for c in server.calls if c["path"].endswith("/partnumber")]
    assert len(exact_calls) == 1
    assert "CRCW060310K0FKEA" in exact_calls[0]["body"]["SearchByPartRequest"]["mouserPartNumber"]


def test_example_bom_resolves_passive_line(client, example_bom):
    items, _ = build_items(load_table(example_bom))
    params = QuoteParams(boards=10, passive_spares_pct=10)
    specs, required = specs_for(items, params)
    assert [s.label() for s in specs] == ["Resistencia 4,7 kΩ ±1 % 0603"]
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    row15 = next(i for i in items if i.rows == [15])
    quote = quote_all([row15], params)[0]
    assert quote.status == "OK · automática"
    assert quote.part.mpn == "CRCW06034K70FKEA"  # reconocido y más barato para 11 unidades
    assert any("Elegida automáticamente" in n for n in quote.notes)
    assert row15.spec_report["accepted"] >= 2


def test_capacitor_without_voltage_is_flagged(client, tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("Ref,Qty,Value,Footprint\nC1 C2,2,100nF,C_0603_1608Metric\n", encoding="utf-8")
    items, _ = build_items(load_table(path))
    params = QuoteParams(boards=10)
    specs, required = specs_for(items, params)
    bundle = lookup_all(client, [], specs, Defaults(cap_voltage=16), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    quote = quote_all(items, params)[0]
    assert quote.part.mpn in ("CL10B104KB8NNNC", "GRM188R71C104KA01D")
    assert quote.level == "warn" and quote.status == "Revisar tensión"


def test_same_spec_lines_are_consolidated(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("Designator,Qty,Value,Footprint,Tolerance\nR1,1,10k,R0603,1%\nR2 R3,2,10K,R_0603_1608Metric,1 %\n"
                    "R4,1,10k,R0402,1%\n", encoding="utf-8")
    items, _ = build_items(load_table(path))
    assert len(items) == 2
    assert items[0].qty_per_board == 3 and items[0].by_spec and items[0].spec.tolerance == 1


def test_disabled_passives_show_clear_status(tmp_path):
    from mouser_engine.quote import mark_specs_disabled

    path = tmp_path / "bom.csv"
    path.write_text("Ref,Qty,Value,Footprint\nR1,1,10k,R0603\n", encoding="utf-8")
    items, _ = build_items(load_table(path))
    mark_specs_disabled(items)
    quote = quote_all(items, QuoteParams())[0]
    assert quote.status == "Sin N° de parte" and "desactivada" in quote.notes[0]
