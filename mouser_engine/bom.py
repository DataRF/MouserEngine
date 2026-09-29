"""Lectura de archivos BOM (CSV / Excel), detección de columnas y consolidación."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import BomItem, BomLine
from .passives import parse_spec
from .utils import clean_cell, normalize_header, normalize_pn, parse_int

FIELDS = ["mpn", "mouser_pn", "manufacturer", "qty", "designators", "description", "value", "footprint"]

FIELD_LABELS = {
    "mpn": "N° de parte del fabricante (MPN)",
    "mouser_pn": "N° de parte Mouser",
    "manufacturer": "Fabricante",
    "qty": "Cantidad por placa",
    "designators": "Designadores",
    "description": "Descripción",
    "value": "Valor",
    "footprint": "Encapsulado",
}

SYNONYMS: dict[str, list[str]] = {
    "mouser_pn": [
        "mouser part number", "mouser part no", "mouser part", "mouser pn", "mouser p n",
        "mouser no", "mouser nr", "mouser number", "mouser", "mouser code", "mouser sku",
        "codigo mouser", "n mouser", "no mouser", "nro mouser", "numero mouser",
        "mouser part number 1", "mouser stock no",
    ],
    "mpn": [
        "mpn", "manufacturer part number", "manufacturer part no", "manufacturer part",
        "manufacturer pn", "manufacturer p n", "mfr part number", "mfr part no", "mfr part",
        "mfr pn", "mfr p n", "mfr no", "mfr number", "mfg part number", "mfg part no",
        "mfg part", "mfg pn", "mfgpn", "mfrpn", "manf part", "manf part number",
        "manufacturer part number 1", "mfr part number 1", "mpn1", "mpn 1", "part number",
        "part no", "part num", "partnumber", "pn", "p n", "numero de parte", "numero parte",
        "n de parte", "n parte", "no de parte", "no parte", "nro de parte", "nro parte",
        "num parte", "numero de parte fabricante", "n parte fabricante", "pn fabricante",
        "codigo fabricante", "codigo de fabricante", "part number fabricante",
        "referencia fabricante", "ref fabricante", "mfr no", "manufacturer part code",
    ],
    "manufacturer": [
        "manufacturer", "manufacturer name", "manufacturer 1", "mfr", "mfr name", "mfg",
        "mfg name", "manf", "maker", "brand", "fabricante", "marca", "vendor", "mf",
        "fabricante 1",
    ],
    "qty": [
        "qty", "quantity", "cantidad", "cant", "qty per board", "qty per pcb", "qty board",
        "qty pcb", "quantity per board", "quantity per pcb", "cantidad por placa",
        "cant por placa", "cant placa", "q ty", "qnty", "qte", "count", "units", "unidades",
        "uds", "qty per unit", "qty per assembly", "quantity per assembly", "cantidad por unidad",
    ],
    "designators": [
        "designator", "designators", "designador", "designadores", "reference", "references",
        "reference designator", "reference designators", "ref des", "refdes", "ref", "refs",
        "part reference", "part references", "parts", "schematic reference", "referencia",
        "referencias", "posicion", "posiciones", "customer reference",
    ],
    "description": [
        "description", "descripcion", "desc", "part description", "item description",
        "detalle", "descripcion del componente",
    ],
    "value": ["value", "valor", "comment", "comentario", "val"],
    "footprint": [
        "footprint", "package", "encapsulado", "pcb footprint", "case", "case package",
        "package case", "huella", "formato",
    ],
}

# Palabras que descartan una columna para un campo al buscar coincidencias parciales.
_EXCLUDE = {
    "mpn": ("mouser", "digi", "digikey", "lcsc", "supplier", "proveedor", "distributor",
            "farnell", "arrow", "newark", "internal", "interno", "customer", "cliente", "jlc"),
    "manufacturer": ("part", "pn", "number", "numero", "codigo"),
    "qty": ("price", "precio", "stock", "moq", "min", "available", "disponible", "cost", "costo"),
    "designators": ("fabricante", "manufacturer", "mfr"),
    "mouser_pn": ("price", "precio", "stock", "url", "link", "qty", "cantidad", "cost", "costo"),
}

# Encabezados escritos con "#" que la normalización confundiría (convención de KiCost y otros).
_RAW_HEADERS = {"manf#": "mpn", "mfr#": "mpn", "mfg#": "mpn", "part#": "mpn", "mpn#": "mpn",
                "mouser#": "mouser_pn"}

# Columnas no asignadas que igual aportan datos técnicos para resistencias y condensadores.
_SPEC_HEADER_WORDS = ("tolerance", "tolerancia", "tol", "voltage", "voltaje", "tension", "volt", "rated",
                      "power", "potencia", "wattage", "watt", "dielectric", "dielectrico", "material",
                      "tempco", "temperature", "coeficiente", "device", "dispositivo", "size", "tamano",
                      "rating", "type", "tipo", "tc")

_MOUSER_PN_RE = re.compile(r"^\d{2,3}-[A-Z0-9][A-Z0-9.\-/#+,]*$", re.IGNORECASE)
_PASSIVE_PREFIXES = {"R", "C", "L", "FB", "RN", "RA"}
_PASSIVE_WORDS = ("resistor", "capacitor", "inductor", "ferrite", "resistencia", "condensador",
                  "bead", "chip res", "chip cap", "mlcc")


class BomError(Exception):
    """El archivo BOM no se pudo leer o interpretar."""


@dataclass
class BomTable:
    path: str
    sheet_names: list[str]
    sheet: str
    grid: list[list[str]]
    header_row: int = 0
    mapping: dict[str, int] = field(default_factory=dict)
    sheets: dict[str, list[list[str]]] = field(default_factory=dict, repr=False)

    @property
    def headers(self) -> list[str]:
        if 0 <= self.header_row < len(self.grid):
            return self.grid[self.header_row]
        return []

    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.grid), default=0)


# --- Lectura de archivos ----------------------------------------------------

def _trim(grid: list[list[str]]) -> list[list[str]]:
    rows = []
    for row in grid:
        row = list(row)
        while row and not row[-1]:
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _guess_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:50])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def _read_csv(path: Path) -> list[list[str]]:
    text = _decode(path.read_bytes())
    delimiter = _guess_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return _trim([[clean_cell(c) for c in row] for row in reader])


def _read_xlsx_sheets(path: Path) -> dict[str, list[list[str]]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependencia obligatoria
        raise BomError("Falta la biblioteca openpyxl para leer archivos Excel.") from exc
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl lanza varios tipos
        raise BomError(f"No se pudo abrir el Excel: {exc}") from exc
    sheets: dict[str, list[list[str]]] = {}
    try:
        for ws in workbook.worksheets[:15]:
            grid = [[clean_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
            sheets[ws.title] = _trim(grid)
    finally:
        workbook.close()
    return sheets


def _read_xls_sheets(path: Path) -> dict[str, list[list[str]]]:
    try:
        import xlrd  # type: ignore
    except ImportError as exc:
        raise BomError("Los archivos .xls antiguos no están soportados. "
                       "Guarde el BOM como .xlsx o .csv desde Excel.") from exc
    book = xlrd.open_workbook(str(path))
    return {sh.name: _trim([[clean_cell(sh.cell_value(r, c)) for c in range(sh.ncols)]
                            for r in range(sh.nrows)]) for sh in book.sheets()}


def read_sheets(path: str | Path) -> dict[str, list[list[str]]]:
    """Lee todas las hojas del archivo como grillas de texto."""
    p = Path(path)
    if not p.exists():
        raise BomError(f"No existe el archivo: {p}")
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return _read_xlsx_sheets(p)
    if ext == ".xls":
        return _read_xls_sheets(p)
    if ext in (".csv", ".txt", ".tsv", ".bom"):
        return {p.stem: _read_csv(p)}
    raise BomError(f"Formato no soportado: {ext}. Use .xlsx, .csv o .txt.")


# --- Detección de encabezados y columnas -------------------------------------

def _field_for_header(header: str) -> str | None:
    for name in FIELDS:
        if header in SYNONYMS[name]:
            return name
    return None


def header_score(row: list[str]) -> int:
    found = set()
    for cell in row:
        name = _field_for_header(normalize_header(cell))
        if name:
            found.add(name)
    return len(found)


def detect_header_row(grid: list[list[str]], max_scan: int = 40) -> int:
    best_row, best_score = 0, 0
    for index, row in enumerate(grid[:max_scan]):
        score = header_score(row)
        if score > best_score:
            best_row, best_score = index, score
    return best_row


def _contains_words(header: str, synonym: str) -> bool:
    return f" {synonym} " in f" {header} "


def auto_map(headers: list[str], rows: list[list[str]] | None = None) -> dict[str, int]:
    """Asocia cada campo conocido a una columna según el texto del encabezado."""
    normalized = [normalize_header(h) for h in headers]
    mapping: dict[str, int] = {}
    used: set[int] = set()

    for index, header in enumerate(headers):
        name = _RAW_HEADERS.get(str(header or "").strip().lower().replace(" ", ""))
        if name and name not in mapping:
            mapping[name] = index
            used.add(index)

    for name in FIELDS_BY_PRIORITY:
        if name in mapping:
            continue
        for synonym in SYNONYMS[name]:
            idx = next((i for i, h in enumerate(normalized) if i not in used and h == synonym), None)
            if idx is not None:
                mapping[name] = idx
                used.add(idx)
                break

    for name in FIELDS_BY_PRIORITY:
        if name in mapping:
            continue
        for synonym in SYNONYMS[name]:
            if len(synonym) < 5:
                continue
            idx = next(
                (i for i, h in enumerate(normalized)
                 if i not in used and h and _contains_words(h, synonym)
                 and not any(word in h.split() for word in _EXCLUDE.get(name, ()))),
                None,
            )
            if idx is not None:
                mapping[name] = idx
                used.add(idx)
                break

    if rows and "mouser_pn" not in mapping:
        idx = _guess_mouser_column(normalized, rows, used)
        if idx is not None:
            mapping["mouser_pn"] = idx
            used.add(idx)
    return mapping


FIELDS_BY_PRIORITY = ["mouser_pn", "mpn", "manufacturer", "qty", "designators", "description",
                      "value", "footprint"]


def _guess_mouser_column(headers: list[str], rows: list[list[str]], used: set[int]) -> int | None:
    """Detecta una columna de proveedor cuyo proveedor es Mouser o cuyos valores lucen como
    códigos Mouser (p. ej. "595-LM358DR")."""
    sample = [r for r in rows[:200] if any(r)]
    if not sample:
        return None
    supplier_cols = [i for i, h in enumerate(headers)
                     if re.fullmatch(r"(supplier|distributor|proveedor|distribuidor|dist)( \d+)?", h)]
    for i, h in enumerate(headers):
        if i in used or not re.search(r"(supplier|distributor|proveedor|distribuidor|\bdist\b|\bdpn\b)", h):
            continue
        if not re.search(r"(part|pn|number|numero|codigo|no\b)", h):
            continue
        suffix = re.search(r"(\d+)$", h)
        for sc in supplier_cols:
            if suffix and not headers[sc].endswith(suffix.group(1)):
                continue
            values = [r[sc].lower() for r in sample if sc < len(r) and r[sc]]
            if values and sum("mouser" in v for v in values) >= len(values) / 2:
                return i
    for i in range(max(len(r) for r in sample)):
        if i in used:
            continue
        values = [r[i] for r in sample if i < len(r) and r[i]]
        if len(values) >= 3 and sum(bool(_MOUSER_PN_RE.match(v)) for v in values) >= 0.8 * len(values):
            return i
    return None


def load_table(path: str | Path, sheet: str | None = None) -> BomTable:
    """Lee el archivo y detecta automáticamente hoja, fila de encabezado y columnas."""
    sheets = read_sheets(path)
    if not sheets:
        raise BomError("El archivo no contiene datos.")
    names = list(sheets)
    if sheet is None or sheet not in sheets:
        scored = [(header_score(sheets[n][detect_header_row(sheets[n])]) if sheets[n] else -1, -i, n)
                  for i, n in enumerate(names)]
        sheet = max(scored)[2]
    grid = sheets[sheet]
    if not grid:
        raise BomError(f"La hoja '{sheet}' está vacía.")
    header_row = detect_header_row(grid)
    table = BomTable(str(path), names, sheet, grid, header_row, sheets=sheets)
    table.mapping = auto_map(table.headers, grid[header_row + 1:])
    return table


def switch_sheet(table: BomTable, sheet: str) -> BomTable:
    sheets = table.sheets or read_sheets(table.path)
    grid = sheets.get(sheet) or []
    header_row = detect_header_row(grid) if grid else 0
    new = BomTable(table.path, list(sheets), sheet, grid, header_row, sheets=sheets)
    new.mapping = auto_map(new.headers, grid[header_row + 1:]) if grid else {}
    return new


def set_header_row(table: BomTable, header_row: int) -> None:
    """Cambia la fila de encabezado y vuelve a detectar las columnas."""
    table.header_row = max(0, min(header_row, max(0, len(table.grid) - 1)))
    table.mapping = auto_map(table.headers, table.grid[table.header_row + 1:])


# --- Interpretación de filas -------------------------------------------------

def split_designators(text: str) -> list[str]:
    text = re.sub(r"\s*[-–]\s*", "-", text or "")
    return [t for t in re.split(r"[,;\s/]+", text) if t]


def count_designators(text: str) -> int:
    count = 0
    for token in split_designators(text):
        match = re.fullmatch(r"([A-Za-z_]+)(\d+)-(?:\1)?(\d+)", token)
        if match and int(match.group(3)) >= int(match.group(2)):
            count += int(match.group(3)) - int(match.group(2)) + 1
        else:
            count += 1
    return count


def is_passive(designators: str, description: str = "") -> bool:
    prefixes = set()
    for token in split_designators(designators):
        match = re.match(r"([A-Za-z]+)\d", token)
        if match:
            prefixes.add(match.group(1).upper())
    if prefixes:
        return prefixes <= _PASSIVE_PREFIXES
    text = normalize_header(description)
    return any(word in text for word in _PASSIVE_WORDS)


_DNP_WORDS = ("dnp", "dni", "dnf", "no montar", "not fitted", "no fit", "nf", "nc")


def parse_lines(table: BomTable) -> tuple[list[BomLine], list[str]]:
    """Convierte la grilla en líneas de BOM. Devuelve (líneas, advertencias)."""
    mapping = table.mapping
    warnings: list[str] = []
    if "mpn" not in mapping and "mouser_pn" not in mapping:
        warnings.append("No se identificó columna de número de parte (MPN o Mouser). "
                        "Asígnela manualmente para poder cotizar.")
    if "qty" not in mapping:
        warnings.append("No hay columna de cantidad: se usará la cantidad de designadores "
                        "(o 1 si no hay designadores).")

    def get(row: list[str], name: str) -> str:
        idx = mapping.get(name)
        if idx is None or idx >= len(row):
            return ""
        return clean_cell(row[idx])

    mapped = set(mapping.values())
    headers = table.headers
    spec_columns = [
        (i, clean_cell(h)) for i, h in enumerate(headers)
        if i not in mapped and clean_cell(h) and any(
            word in normalize_header(h).split() for word in _SPEC_HEADER_WORDS)
    ]

    lines: list[BomLine] = []
    for index in range(table.header_row + 1, len(table.grid)):
        row = table.grid[index]
        if not any(row):
            continue
        line = BomLine(
            row=index + 1,
            designators=get(row, "designators"),
            mpn=get(row, "mpn"),
            manufacturer=get(row, "manufacturer"),
            mouser_pn=get(row, "mouser_pn"),
            description=get(row, "description"),
            value=get(row, "value"),
            footprint=get(row, "footprint"),
            raw=list(row),
            extra={header: clean_cell(row[i]) for i, header in spec_columns
                   if i < len(row) and clean_cell(row[i])},
        )
        if not (line.mpn or line.mouser_pn or line.designators or line.description or line.value):
            continue
        if normalize_header(line.mpn) in SYNONYMS["mpn"] and normalize_header(line.mpn):
            continue  # encabezado repetido
        qty_text = get(row, "qty")
        if qty_text:
            qty = parse_int(qty_text)
            if qty is None:
                qty = 0 if normalize_header(qty_text) in _DNP_WORDS else (
                    count_designators(line.designators) if line.designators else 0)
        elif line.designators:
            qty = count_designators(line.designators)
        else:
            qty = 1 if (line.mpn or line.mouser_pn) else 0
        line.qty = max(0, qty)
        lines.append(line)
    if not lines:
        warnings.append("No se encontraron filas con datos bajo el encabezado.")
    return lines, warnings


def consolidate(lines: list[BomLine]) -> list[BomItem]:
    """Agrupa líneas con el mismo número de parte para cotizarlas como una sola compra.

    Las resistencias y condensadores sin MPN se agrupan por especificación (mismo valor,
    encapsulado, tolerancia, etc.) para comprarlos juntos y aprovechar los tramos de precio.
    """
    items: dict[str, BomItem] = {}
    for line in lines:
        spec = None
        if not (line.mpn or line.mouser_pn):
            spec = parse_spec(line.designators, line.value, line.description, line.footprint, line.extra)
        if line.qty <= 0:
            key = f"Z:{line.row}"  # no montar: queda aparte y excluida
        elif line.mouser_pn:
            key = "M:" + normalize_pn(line.mouser_pn)
        elif line.mpn:
            key = "P:" + normalize_pn(line.mpn)
        elif spec is not None and spec.complete:
            key = "S:" + spec.key
        else:
            key = f"R:{line.row}"
        item = items.get(key)
        if item is None:
            items[key] = BomItem(
                id=len(items) + 1,
                rows=[line.row],
                designators=line.designators,
                mpn=line.mpn,
                manufacturer=line.manufacturer,
                mouser_pn=line.mouser_pn,
                description=line.description,
                value=line.value,
                footprint=line.footprint,
                qty_per_board=line.qty,
                extra=dict(line.extra),
                spec=spec,
            )
            continue
        item.rows.append(line.row)
        if line.designators:
            item.designators = ", ".join(x for x in (item.designators, line.designators) if x)
        item.qty_per_board += line.qty
        for attr in ("mpn", "manufacturer", "mouser_pn", "description", "value", "footprint"):
            if not getattr(item, attr) and getattr(line, attr):
                setattr(item, attr, getattr(line, attr))

    result = list(items.values())
    for item in result:
        item.is_passive = is_passive(item.designators, item.description or item.value)
        if item.qty_per_board <= 0:
            item.include = False
        if not item.base_query:
            item.lookup_state = "pending" if (item.spec is not None and item.spec.complete) else "noquery"
    return result


def build_items(table: BomTable) -> tuple[list[BomItem], list[str]]:
    lines, warnings = parse_lines(table)
    return consolidate(lines), warnings


def keyword_for(item: BomItem) -> str:
    """Texto sugerido para buscar una parte sin MPN en Mouser."""
    if item.mpn or item.mouser_pn:
        return item.mouser_pn or item.mpn
    parts = [item.value, item.footprint, item.description]
    return " ".join(p for p in parts if p).strip()[:80]
