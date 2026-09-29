import unicodedata
from datetime import datetime
from decimal import Decimal

import pytest
from pypdf import PdfReader

from mouser_engine.bom import build_items, load_table
from mouser_engine.gui.pdf_report import ReportSections, write_client_report
from mouser_engine.landed import ExchangeRates, ImportSetup, builtin_rules
from mouser_engine.lookup import lookup_all
from mouser_engine.models import QuoteParams
from mouser_engine.passives import Defaults
from mouser_engine.quote import apply_lookup, queries_for, specs_for
from mouser_engine.report import build_client_report


def make_report(client, example_bom, **params):
    items, _ = build_items(load_table(example_bom))
    p = QuoteParams(**{"boards": 10, "passive_spares_pct": 10, **params})
    specs, required = specs_for(items, p)
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    return build_client_report(items, p, [1, 10, 25, 50, 100, 500, 1000], 1000, client="Cliente Ejemplo",
                               project="Placa de control", queried_at=datetime(2026, 9, 29, 12, 0))


@pytest.fixture
def report(qapp, client, example_bom):
    return make_report(client, example_bom)


def pages_text(path):
    reader = PdfReader(str(path))
    return reader, [page.extract_text() or "" for page in reader.pages]


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def test_full_report(report, tmp_path):
    path = tmp_path / "informe.pdf"
    pages = write_client_report(report, path)
    reader, texts = pages_text(path)
    assert pages == len(reader.pages) >= 2
    first = normalized(texts[0])
    assert "Análisis de costo por volumen de fabricación" in first
    assert "Cliente Ejemplo" in first and "Placa de control" in first
    assert "Escenarios comparados" in first and "Costo según la cantidad a fabricar" in first
    assert f"Página 1 de {pages}" in first
    everything = normalized(" ".join(texts))
    for heading in ("Resumen de costos a 10 placas", "Partes que más influyen en el costo", "Observaciones", "Notas"):
        assert heading in everything, heading
    assert "STM32F103C8T6" in everything and "LM358DR" in everything
    # análisis comercial: sin el BOM (detalle de partes, designadores, precios de cada parte por cantidad)
    for bom in ("Detalle de partes", "Precio unitario según la cantidad", "Designadores", "C1, C2"):
        assert bom not in everything, bom
    assert "Solo componentes (precio Mouser)" in first
    assert "no incluyen flete, derechos de aduana, IVA ni desaduanamiento" in everything
    assert "supone que habrá stock" in everything
    assert f"Página {pages} de {pages}" in normalized(texts[-1])
    width = float(reader.pages[0].mediabox.width)
    assert abs(width - 612) < 1  # carta: 8,5 pulgadas
    assert reader.metadata.title.startswith("Análisis de costo por volumen")


def test_a4_split_and_sections(report, tmp_path):
    report.chart_mode = "split"
    path = tmp_path / "corto.pdf"
    pages = write_client_report(report, path, page_size="a4",
                                sections=ReportSections(top_parts=False, observations=False))
    reader, texts = pages_text(path)
    assert abs(float(reader.pages[0].mediabox.width) - 595) < 1
    everything = normalized(" ".join(texts))
    assert "Escenarios comparados" in everything and "Notas" in everything and "Resumen de costos" in everything
    assert "Partes que más influyen" not in everything and "Observaciones" not in everything
    assert pages <= 2


def test_totals_with_costs_and_clp(qapp, client, example_bom, tmp_path):
    report = make_report(client, example_bom, freight=40, vat_pct=19, fx_rate=950)
    report.client = "Una empresa con un nombre muy largo para ver que el pie de página no se desborde S.A."
    path = tmp_path / "costos.pdf"
    write_client_report(report, path)
    everything = normalized(" ".join(pages_text(path)[1]))
    for text in ("Flete estimado", "IVA (19 %)", "Total", "Total en pesos chilenos: CLP",
                 "Los costos incluyen flete estimado"):
        assert text in everything, text


def test_landed_cost_report(qapp, client, example_bom, tmp_path):
    setup = ImportSetup(builtin_rules(), ExchangeRates(usd=Decimal("942.25"), usd_date="2026-09-29",
                                                       customs=Decimal("933.9"), customs_date="2026-08-28",
                                                       customs_month="2026-09"))
    report = make_report(client, example_bom, landed=True, import_setup=setup)
    cost = report.summary.landed
    assert cost is not None and report.summary.total == cost.total
    path = tmp_path / "puesto.pdf"
    write_client_report(report, path)
    _, texts = pages_text(path)
    first = normalized(texts[0])
    assert "Puestos en Chile (todo incluido)" in first and "10 placas, todo incluido" in first
    everything = normalized(" ".join(texts))
    for text in ("Flete de Mouser", "Derechos de aduana (6 % del valor CIF)", "IVA de importación (19 %)",
                 "Honorario de desaduanamiento (DHL)", "IVA del honorario (19 %)", "Total puesto en Chile",
                 "IVA incluido (crédito fiscal)", "Total sin IVA", "Total en pesos chilenos: CLP",
                 "dólar observado de 942,25 CLP del 29-09-2026", "dólar aduanero de 933,90 CLP (septiembre)",
                 "Los costos son puestos en Chile"):
        assert text in everything, text


def test_unwritable_path(report, tmp_path):
    with pytest.raises(OSError):
        write_client_report(report, tmp_path)  # es una carpeta
