import unicodedata
from datetime import datetime

import pytest
from pypdf import PdfReader

from mouser_engine.bom import build_items, load_table
from mouser_engine.gui.pdf_report import ReportSections, write_client_report
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
    assert pages == len(reader.pages) >= 3
    first = normalized(texts[0])
    assert "Análisis de costo por volumen de fabricación" in first
    assert "Cliente Ejemplo" in first and "Placa de control" in first
    assert "Escenarios comparados" in first and "Costo según la cantidad a fabricar" in first
    assert f"Página 1 de {pages}" in first
    everything = normalized(" ".join(texts))
    for heading in ("Partes que más influyen en el costo", "Observaciones", "Precio unitario según la cantidad",
                    "Detalle de partes a 10 placas", "Notas"):
        assert heading in everything, heading
    assert "STM32F103C8T6" in everything and "LM358DR" in everything
    assert f"Página {pages} de {pages}" in normalized(texts[-1])
    width = float(reader.pages[0].mediabox.width)
    assert abs(width - 612) < 1  # carta: 8,5 pulgadas
    assert reader.metadata.title.startswith("Análisis de costo por volumen")


def test_a4_split_and_sections(report, tmp_path):
    report.chart_mode = "split"
    path = tmp_path / "corto.pdf"
    pages = write_client_report(report, path, page_size="a4",
                                sections=ReportSections(top_parts=False, observations=False, price_matrix=False,
                                                        detail=False))
    reader, texts = pages_text(path)
    assert abs(float(reader.pages[0].mediabox.width) - 595) < 1
    everything = normalized(" ".join(texts))
    assert "Escenarios comparados" in everything and "Notas" in everything
    assert "Detalle de partes" not in everything and "Observaciones" not in everything
    assert pages <= 2


def test_totals_with_costs_and_clp(qapp, client, example_bom, tmp_path):
    report = make_report(client, example_bom, freight=40, vat_pct=19, fx_rate=950)
    report.client = "Una empresa con un nombre muy largo para ver que el pie de página no se desborde S.A."
    path = tmp_path / "costos.pdf"
    write_client_report(report, path)
    everything = normalized(" ".join(pages_text(path)[1]))
    for text in ("Flete estimado", "IVA (19 %)", "Total estimado", "Total estimado en pesos chilenos", "CLP",
                 "Los costos incluyen flete estimado"):
        assert text in everything, text


def test_unwritable_path(report, tmp_path):
    with pytest.raises(OSError):
        write_client_report(report, tmp_path)  # es una carpeta
