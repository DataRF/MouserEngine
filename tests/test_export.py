import csv
from datetime import datetime
from decimal import Decimal

from openpyxl import load_workbook

from mouser_engine.bom import build_items, load_table
from mouser_engine.export import cart_rows, export_cart_csv, export_excel
from mouser_engine.landed import ExchangeRates, ImportSetup, builtin_rules
from mouser_engine.models import QuoteParams
from mouser_engine.quote import apply_lookup, queries_for, quote_all, summarize


def build(client, example_bom, **params):
    table = load_table(example_bom)
    items, _ = build_items(table)
    apply_lookup(items, client.lookup(queries_for(items)))
    p = QuoteParams(**params)
    quotes = quote_all(items, p)
    return table, items, quotes, summarize(items, quotes, p), p


def test_export_excel(client, example_bom, tmp_path):
    table, items, quotes, summary, params = build(client, example_bom, boards=10, vat_pct=19, fx_rate=900)
    path = export_excel(tmp_path / "cot.xlsx", items, quotes, summary, params, bom_path=str(example_bom),
                        bom_grid=table.grid, queried_at=datetime(2026, 9, 29, 12, 30))
    wb = load_workbook(path)
    assert wb.sheetnames == ["Resumen", "Detalle", "Problemas", "Carro Mouser", "BOM original"]
    resumen = {row[0]: row[1] for row in wb["Resumen"].iter_rows(min_row=3, values_only=True) if row[0]}
    assert resumen["Placas / equipos"] == 10
    assert resumen["Precios y stock consultados"] == "29-09-2026 12:30"
    assert abs(resumen["Total estimado"] - float(summary.total)) < 0.001
    assert "Total estimado en CLP" in resumen
    detail = wb["Detalle"]
    headers = [c.value for c in detail[1]]
    assert headers[:3] == ["Ítem", "Líneas BOM", "Comprar"]
    rows = list(detail.iter_rows(min_row=2, values_only=True))
    assert len(rows) == len(items)
    first = dict(zip(headers, rows[0]))
    assert first["N° Mouser"] == "81-GRM188R71C104KA1D"
    assert first["Cant. a comprar"] == 80 and first["Total línea"] == 1.68
    assert first["Empaque"] == "Carrete, Cinta cortada, MouseReel"
    link_col = headers.index("Link Mouser") + 1
    assert detail.cell(row=2, column=link_col).hyperlink.target.startswith("https://www.mouser.com/")
    problems = list(wb["Problemas"].iter_rows(min_row=2, values_only=True))
    assert len(problems) == summary.warn + summary.error
    assert wb["BOM original"]["A4"].value == "Designador"


def test_export_excel_with_landed_cost(client, example_bom, tmp_path):
    setup = ImportSetup(builtin_rules(), ExchangeRates(usd=Decimal("942.25"), usd_date="2026-09-29",
                                                       customs=Decimal("933.9"), customs_month="2026-09"))
    table, items, quotes, summary, params = build(client, example_bom, boards=10, landed=True, import_setup=setup)
    path = export_excel(tmp_path / "cot.xlsx", items, quotes, summary, params, scenario_quantities=[1, 10, 100],
                        scenario_max=100)
    wb = load_workbook(path)
    resumen = {row[0]: row[1] for row in wb["Resumen"].iter_rows(min_row=3, values_only=True) if row[0]}
    cost = summary.landed
    assert abs(resumen["Total puesto en Chile (todo incluido)"] - float(cost.total)) < 0.001
    assert abs(resumen["Honorario de desaduanamiento DHL"] - float(cost.brokerage)) < 0.001
    assert abs(resumen["Derechos de aduana (6 % del CIF)"] - float(cost.duty)) < 0.001
    assert resumen["Total puesto en Chile en CLP"] == float(cost.total_clp)
    assert resumen["Dólar observado (CLP) del 29-09-2026"] == 942.25
    assert resumen["Dólar aduanero (CLP) de septiembre"] == 933.9
    assert "Total estimado" not in resumen
    notes = " ".join(str(c.value) for row in wb["Resumen"].iter_rows() for c in row if isinstance(c.value, str))
    assert "puesto en Chile suma el flete" in notes
    escenarios = wb["Escenarios"]
    assert "puesto en Chile" in escenarios["A2"].value and "supone stock" in escenarios["A2"].value


def test_cart_rows_and_csv(client, example_bom, tmp_path):
    _, items, quotes, _, _ = build(client, example_bom, boards=10)
    rows = cart_rows(items, quotes)
    pns = [r[0] for r in rows]
    assert "81-GRM188R71C104KA1D" in pns
    assert "700-MAX232CPE" not in pns          # sin precio
    assert pns.count("81-GRM188R71C104KA1D") == 1  # la línea DNP no se compra
    assert all(len(r[2]) <= 21 and "*" not in r[2] for r in rows)  # límite de la Cart API
    path = tmp_path / "carro.csv"
    assert export_cart_csv(path, items, quotes) == len(rows)
    with path.open(encoding="utf-8-sig") as fh:
        data = list(csv.reader(fh))
    assert data[0][:2] == ["Mouser Part Number", "Quantity"]
    assert data[1][:2] == ["81-GRM188R71C104KA1D", "80"]


def test_export_excel_scenarios(client, example_bom, tmp_path):
    _, items, quotes, summary, params = build(client, example_bom, boards=10, passive_spares_pct=10)
    path = export_excel(tmp_path / "cot.xlsx", items, quotes, summary, params,
                        scenario_quantities=[1, 10, 25, 50, 100, 500, 1000], scenario_max=1000,
                        client_name="Cliente de prueba", company_name="FARADIUM SPA")
    wb = load_workbook(path)
    assert wb.sheetnames == ["Resumen", "Escenarios", "Detalle", "Problemas", "Carro Mouser"]
    resumen = {row[0]: row[1] for row in wb["Resumen"].iter_rows(min_row=3, values_only=True) if row[0]}
    assert resumen["Empresa"] == "FARADIUM SPA" and resumen["Cliente"] == "Cliente de prueba"
    sheet = wb["Escenarios"]
    assert "Cliente de prueba" in sheet["A2"].value
    headers = [c.value for c in sheet[4]][:8]
    assert headers[0] == "Placas" and headers[1] == "Costo por placa (USD)"
    rows = [r[:8] for r in sheet.iter_rows(min_row=5, max_row=11, values_only=True)]
    assert [r[0] for r in rows] == [1, 10, 25, 50, 100, 500, 1000]
    ten = dict(zip(headers, rows[1]))
    assert abs(ten["Costo total (USD)"] - float(summary.total)) < 0.001  # 10 placas = cotización actual
    assert rows[0][2] is None and rows[1][2] < -0.1  # variación en fracción (formato %)
    assert sheet.cell(row=6, column=3).font.b  # baja importante destacada
    assert len(sheet._charts) == 2
    # los gráficos toman los datos de la curva (a la derecha de la tabla)
    curve_boards = [r[0] for r in sheet.iter_rows(min_row=5, min_col=10, max_col=10, values_only=True) if r[0]]
    assert curve_boards[0] == 1 and curve_boards[-1] == 1000 and len(curve_boards) > 50
