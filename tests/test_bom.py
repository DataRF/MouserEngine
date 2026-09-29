import pytest
from openpyxl import Workbook

from mouser_engine.bom import (
    BomError,
    auto_map,
    build_items,
    count_designators,
    detect_header_row,
    is_passive,
    load_table,
    switch_sheet,
)


def names(mapping, headers):
    return {field: headers[idx] for field, idx in mapping.items()}


@pytest.mark.parametrize("headers,expected", [
    # Altium
    (["Comment", "Description", "Designator", "Footprint", "LibRef", "Quantity", "Manufacturer 1",
      "Manufacturer Part Number 1", "Supplier 1", "Supplier Part Number 1"],
     {"mpn": "Manufacturer Part Number 1", "manufacturer": "Manufacturer 1", "qty": "Quantity",
      "designators": "Designator", "description": "Description", "value": "Comment",
      "footprint": "Footprint"}),
    # KiCad
    (["Reference", "Value", "Footprint", "Qty", "DNP", "MPN", "Manufacturer"],
     {"mpn": "MPN", "manufacturer": "Manufacturer", "qty": "Qty", "designators": "Reference",
      "value": "Value", "footprint": "Footprint"}),
    # JLCPCB / EasyEDA
    (["Comment", "Designator", "Footprint", "LCSC Part #", "Manufacturer Part", "Manufacturer", "Quantity"],
     {"mpn": "Manufacturer Part", "manufacturer": "Manufacturer", "qty": "Quantity",
      "designators": "Designator", "value": "Comment", "footprint": "Footprint"}),
    # Exportación de Mouser
    (["Mouser No", "Mfr. No", "Manufacturer", "Customer No", "Description", "RoHS", "Quantity", "Price"],
     {"mouser_pn": "Mouser No", "mpn": "Mfr. No", "manufacturer": "Manufacturer", "qty": "Quantity",
      "description": "Description"}),
    # Español
    (["Ítem", "Designador", "Cant.", "Fabricante", "N° de parte", "Descripción"],
     {"mpn": "N° de parte", "manufacturer": "Fabricante", "qty": "Cant.", "designators": "Designador",
      "description": "Descripción"}),
    # Digi-Key: el código de Digi-Key no debe tomarse como MPN
    (["Digi-Key Part Number", "Manufacturer Part Number", "Quantity", "Customer Reference"],
     {"mpn": "Manufacturer Part Number", "qty": "Quantity", "designators": "Customer Reference"}),
])
def test_auto_map(headers, expected):
    assert names(auto_map(headers), headers) == expected


def test_auto_map_supplier_columns_with_mouser_values():
    headers = ["Designator", "Quantity", "Manufacturer Part Number 1", "Supplier 1", "Supplier Part Number 1"]
    rows = [["R1", "1", "RC0603FR-0710KL", "Mouser", "603-RC0603FR-0710KL"],
            ["C1", "1", "GRM188R71C104KA01D", "Mouser", "81-GRM188R71C104KA1D"]]
    mapping = auto_map(headers, rows)
    assert headers[mapping["mouser_pn"]] == "Supplier Part Number 1"


def test_auto_map_detects_mouser_codes_by_pattern():
    headers = ["Ref", "Cantidad", "Codigo"]
    rows = [["R1", "1", "603-RC0603FR-0710KL"], ["C1", "2", "81-GRM188R71C104KA1D"],
            ["U1", "1", "595-LM358DR"]]
    assert auto_map(headers, rows)["mouser_pn"] == 2


def test_detect_header_after_title_rows():
    grid = [["Proyecto X"], ["Revisión B"], [], ["Designador", "Cantidad", "Fabricante", "N° de parte"],
            ["R1", "1", "Yageo", "RC0603FR-0710KL"]]
    assert detect_header_row(grid) == 3


@pytest.mark.parametrize("text,expected", [
    ("R1, R2, R3", 3), ("R1-R10", 10), ("R1 - R4, R8", 5), ("C1 C2 C3", 3), ("U1", 1), ("", 0),
    ("R10-R1", 1), ("U1-R3", 1), ("R1-10", 10),
])
def test_count_designators(text, expected):
    assert count_designators(text) == expected


def test_is_passive():
    assert is_passive("R1, R2, C3")
    assert is_passive("FB1")
    assert not is_passive("R1, U2")
    assert not is_passive("J1")
    assert is_passive("", "Resistencia 10k 0603")


def test_example_bom(example_bom):
    table = load_table(example_bom)
    assert table.header_row == 3
    items, warnings = build_items(table)
    assert warnings == []
    assert len(items) == 12
    cap = items[0]
    assert cap.mpn == "GRM188R71C104KA01D" and cap.qty_per_board == 8 and cap.is_passive
    resistors = items[1]
    assert resistors.qty_per_board == 10
    no_pn = next(i for i in items if i.rows == [15])
    assert no_pn.lookup_state == "noquery"
    dnp = items[-1]
    assert dnp.qty_per_board == 0 and not dnp.include and dnp.rows == [16]


def test_consolidates_duplicate_part_numbers(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("Ref,Qty,MPN\nR1,1,RC0603FR-0710KL\nR2 R3,2,rc0603fr-0710kl\nU1,1,LM358DR\n",
                    encoding="utf-8")
    items, _ = build_items(load_table(path))
    assert len(items) == 2
    assert items[0].qty_per_board == 3
    assert items[0].rows == [2, 3]
    assert items[0].designators == "R1, R2 R3"


def test_quantity_from_designators_when_missing(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("Designator;MPN\nC1,C2,C3;GRM188R71C104KA01D\n", encoding="cp1252")
    table = load_table(path)
    items, warnings = build_items(table)
    assert items[0].qty_per_board == 3
    assert any("cantidad" in w for w in warnings)


def test_semicolon_csv_with_latin1(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes("Designador;Cantidad;Descripción;N° de parte\nU1;2;Módulo;ESP32-WROOM-32E-N4\n"
                     .encode("cp1252"))
    items, _ = build_items(load_table(path))
    assert items[0].description == "Módulo"
    assert items[0].qty_per_board == 2


def test_xlsx_picks_bom_sheet(tmp_path):
    wb = Workbook()
    cover = wb.active
    cover.title = "Portada"
    cover["A1"] = "Cotización placa"
    bom = wb.create_sheet("BOM")
    bom.append(["Qty", "Manufacturer", "MPN", "Designator"])
    bom.append([4, "Murata", "GRM188R71C104KA01D", "C1-C4"])
    bom.append([1.0, "TI", "LM358DR", "U1"])
    path = tmp_path / "bom.xlsx"
    wb.save(path)
    table = load_table(path)
    assert table.sheet == "BOM"
    assert table.sheet_names == ["Portada", "BOM"]
    items, _ = build_items(table)
    assert [i.qty_per_board for i in items] == [4, 1]
    other = switch_sheet(table, "Portada")
    assert other.sheet == "Portada"


def test_unsupported_format(tmp_path):
    path = tmp_path / "bom.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(BomError):
        load_table(path)
