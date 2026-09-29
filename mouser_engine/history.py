"""Historial de cotizaciones en este PC (SQLite, en la carpeta de configuración).

Cada cotización guardada conserva el BOM consolidado, los parámetros y las partes tal como
las devolvió Mouser, para volver a abrirla con los precios de ese momento o compararla con
los de hoy. También registra el precio y stock de cada parte comprada, con lo que se puede
ver cómo cambió una parte a lo largo del tiempo.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import config_dir
from .models import LEVEL_EXCLUDED, BomItem, ItemQuote, Part, QuoteParams
from .passives import parse_spec
from .quote import quote_all, summarize
from .utils import normalize_pn

DB_NAME = "historial.sqlite3"
SNAPSHOT_VERSION = 1
REASONS = {"manual": "Guardada", "excel": "Exportada a Excel", "cart": "Carro creado en Mouser"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    bom_name TEXT NOT NULL DEFAULT '',
    bom_path TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT '',
    boards INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT '',
    goods TEXT NOT NULL DEFAULT '0',
    total TEXT NOT NULL DEFAULT '0',
    parts INTEGER NOT NULL DEFAULT 0,
    priced INTEGER NOT NULL DEFAULT 0,
    unpriced INTEGER NOT NULL DEFAULT 0,
    cart_key TEXT NOT NULL DEFAULT '',
    queried_at TEXT,
    snapshot BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    queried_at TEXT,
    mouser_key TEXT NOT NULL DEFAULT '',
    mpn_key TEXT NOT NULL DEFAULT '',
    mouser_pn TEXT NOT NULL DEFAULT '',
    mpn TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    stock INTEGER,
    required INTEGER NOT NULL DEFAULT 0,
    buy_qty INTEGER NOT NULL DEFAULT 0,
    unit_price TEXT,
    currency TEXT NOT NULL DEFAULT '',
    breaks TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS prices_by_mouser ON prices(mouser_key);
CREATE INDEX IF NOT EXISTS prices_by_mpn ON prices(mpn_key);
CREATE INDEX IF NOT EXISTS prices_by_quote ON prices(quote_id);
"""


class HistoryError(Exception):
    """No se pudo leer o escribir el historial."""


# --- Datos --------------------------------------------------------------------------

@dataclass
class Snapshot:
    """Todo lo necesario para reabrir una cotización tal como estaba."""

    items: list[BomItem]
    params: QuoteParams
    bom_path: str = ""
    client: str = ""
    queried_at: datetime | None = None
    bom_grid: list[list[str]] | None = None
    bom_sheet: str = ""
    bom_header_row: int = 0
    bom_mapping: dict[str, int] = field(default_factory=dict)

    @property
    def bom_name(self) -> str:
        return Path(self.bom_path).name if self.bom_path else ""


@dataclass
class HistoryEntry:
    id: int
    created_at: datetime
    reason: str
    bom_name: str
    bom_path: str
    client: str
    boards: int
    currency: str
    goods: Decimal
    total: Decimal
    parts: int
    priced: int
    unpriced: int
    cart_key: str
    queried_at: datetime | None

    @property
    def reason_label(self) -> str:
        return REASONS.get(self.reason, self.reason)


@dataclass
class PriceRecord:
    """Precio y stock de una parte en una cotización guardada."""

    quote_id: int
    created_at: datetime
    queried_at: datetime | None
    bom_name: str
    client: str
    mouser_pn: str
    mpn: str
    manufacturer: str
    stock: int | None
    required: int
    buy_qty: int
    unit_price: Decimal | None
    currency: str
    breaks: list[tuple[int, Decimal]]

    @property
    def when(self) -> datetime:
        return self.queried_at or self.created_at


# --- Serialización -------------------------------------------------------------------

def part_to_api(part: Part) -> dict:
    """La parte en el formato de la Search API (el original si se tiene)."""
    if part.raw:
        return part.raw
    return {
        "MouserPartNumber": part.mouser_pn, "ManufacturerPartNumber": part.mpn, "Manufacturer": part.manufacturer,
        "Description": part.description, "AvailabilityInStock": part.stock, "Availability": part.availability,
        "FactoryStock": part.factory_stock, "LeadTime": part.lead_time, "LifecycleStatus": part.lifecycle,
        "IsDiscontinued": "true" if part.discontinued else "false", "ROHSStatus": part.rohs,
        "Min": part.min_qty, "Mult": part.mult,
        "PriceBreaks": [{"Quantity": b.quantity, "Price": b.text or str(b.price), "Currency": b.currency}
                        for b in part.price_breaks],
        "AvailabilityOnOrder": [{"Quantity": o.quantity, "Date": o.date} for o in part.on_order],
        "ProductDetailUrl": part.product_url, "DataSheetUrl": part.datasheet_url, "ImagePath": part.image_url,
        "Category": part.category,
        "ProductAttributes": [{"AttributeName": "Packaging", "AttributeValue": v.strip()}
                              for v in part.packaging.split(",") if v.strip()],
        "SuggestedReplacement": part.suggested_replacement,
        "AlternatePackagings": [{"APMfrPN": a} for a in part.alternate_packagings],
        "InfoMessages": list(part.info_messages), "RestrictionMessage": part.restriction,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _dec(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


_ITEM_TEXT = ("designators", "mpn", "manufacturer", "mouser_pn", "description", "value", "footprint",
              "lookup_state", "lookup_error")


def snapshot_to_json(snapshot: Snapshot) -> dict:
    parts: dict[str, dict] = {}

    def ref(part: Part | None) -> str | None:
        if part is None:
            return None
        key = part.key
        parts.setdefault(key, part_to_api(part))
        return key

    items = []
    for item in snapshot.items:
        data = {name: getattr(item, name) for name in _ITEM_TEXT}
        data.update({
            "id": item.id, "rows": list(item.rows), "qty_per_board": item.qty_per_board,
            "is_passive": item.is_passive, "include": item.include, "looked_up_at": _iso(item.looked_up_at),
            "extra": dict(item.extra), "spec_report": item.spec_report,
            "candidates": [ref(p) for p in item.candidates], "near": [ref(p) for p in item.near],
            "suggestions": [ref(p) for p in item.suggestions], "manual_part": ref(item.manual_part),
        })
        items.append(data)
    params = {f.name: getattr(snapshot.params, f.name) for f in fields(QuoteParams)}
    return {
        "version": SNAPSHOT_VERSION,
        "bom_path": snapshot.bom_path,
        "client": snapshot.client,
        "queried_at": _iso(snapshot.queried_at),
        "params": params,
        "bom": {"grid": snapshot.bom_grid, "sheet": snapshot.bom_sheet, "header_row": snapshot.bom_header_row,
                "mapping": snapshot.bom_mapping},
        "items": items,
        "parts": parts,
    }


def snapshot_from_json(data: dict) -> Snapshot:
    if int(data.get("version") or 0) > SNAPSHOT_VERSION:
        raise HistoryError("La cotización fue guardada con una versión más nueva de la aplicación.")
    cache = {key: Part.from_api(raw) for key, raw in (data.get("parts") or {}).items() if isinstance(raw, dict)}

    def parts(keys: list | None) -> list[Part]:
        return [cache[k] for k in keys or [] if k in cache]

    items = []
    for raw in data.get("items") or []:
        item = BomItem(id=int(raw.get("id") or len(items) + 1), rows=[int(r) for r in raw.get("rows") or []])
        for name in _ITEM_TEXT:
            setattr(item, name, str(raw.get(name) or getattr(item, name)))
        item.qty_per_board = int(raw.get("qty_per_board") or 0)
        item.is_passive = bool(raw.get("is_passive"))
        item.include = bool(raw.get("include", True))
        item.looked_up_at = _date(raw.get("looked_up_at"))
        item.extra = {str(k): str(v) for k, v in (raw.get("extra") or {}).items()}
        item.spec_report = raw.get("spec_report")
        item.candidates = parts(raw.get("candidates"))
        item.near = parts(raw.get("near"))
        item.suggestions = parts(raw.get("suggestions"))
        item.manual_part = cache.get(raw.get("manual_part")) if raw.get("manual_part") else None
        if not (item.mpn or item.mouser_pn):  # igual que al importar el BOM
            item.spec = parse_spec(item.designators, item.value, item.description, item.footprint, item.extra)
        items.append(item)
    saved = data.get("params") or {}
    params = QuoteParams(**{f.name: saved[f.name] for f in fields(QuoteParams) if f.name in saved})
    bom = data.get("bom") or {}
    return Snapshot(
        items=items, params=params, bom_path=str(data.get("bom_path") or ""), client=str(data.get("client") or ""),
        queried_at=_date(data.get("queried_at")), bom_grid=bom.get("grid"), bom_sheet=str(bom.get("sheet") or ""),
        bom_header_row=int(bom.get("header_row") or 0),
        bom_mapping={str(k): int(v) for k, v in (bom.get("mapping") or {}).items()},
    )


# --- Almacén -------------------------------------------------------------------------

class HistoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else config_dir() / DB_NAME

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            raise HistoryError(f"No se pudo abrir el historial ({self.path}): {exc}") from exc
        return conn

    def save(self, snapshot: Snapshot, reason: str = "manual", cart_key: str = "",
             when: datetime | None = None) -> int:
        """Guarda la cotización y el precio de cada parte comprada. Devuelve el número de registro."""
        when = when or datetime.now()
        quotes = quote_all(snapshot.items, snapshot.params)
        summary = summarize(snapshot.items, quotes, snapshot.params)
        payload = json.dumps(snapshot_to_json(snapshot), ensure_ascii=False, separators=(",", ":"), default=str)
        blob = zlib.compress(payload.encode("utf-8"), 6)
        try:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO quotes (created_at, reason, bom_name, bom_path, client, boards, currency, goods, "
                    "total, parts, priced, unpriced, cart_key, queried_at, snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_iso(when), reason, snapshot.bom_name, snapshot.bom_path, snapshot.client,
                     snapshot.params.boards, summary.currency, str(summary.goods), str(summary.total),
                     summary.included, summary.priced, summary.unpriced, cart_key, _iso(snapshot.queried_at), blob))
                quote_id = int(cursor.lastrowid)
                rows = []
                for item, q in zip(snapshot.items, quotes):
                    part = q.part
                    if part is None or not item.include or q.level == LEVEL_EXCLUDED:
                        continue
                    breaks = json.dumps([[b.quantity, str(b.price)] for b in part.price_breaks])
                    rows.append((quote_id, _iso(when), _iso(item.looked_up_at or snapshot.queried_at),
                                 normalize_pn(part.mouser_pn), normalize_pn(part.mpn), part.mouser_pn, part.mpn,
                                 part.manufacturer, part.stock, q.required, q.buy_qty,
                                 str(q.unit_price) if q.unit_price is not None else None, part.currency, breaks))
                conn.executemany(
                    "INSERT INTO prices (quote_id, created_at, queried_at, mouser_key, mpn_key, mouser_pn, mpn, "
                    "manufacturer, stock, required, buy_qty, unit_price, currency, breaks) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        except sqlite3.Error as exc:
            raise HistoryError(f"No se pudo guardar en el historial: {exc}") from exc
        return quote_id

    _ENTRY_COLUMNS = ("id, created_at, reason, bom_name, bom_path, client, boards, currency, goods, total, parts, "
                      "priced, unpriced, cart_key, queried_at")

    @staticmethod
    def _entry(r: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            id=r["id"], created_at=_date(r["created_at"]) or datetime.min, reason=r["reason"],
            bom_name=r["bom_name"], bom_path=r["bom_path"], client=r["client"], boards=r["boards"],
            currency=r["currency"], goods=_dec(r["goods"]) or Decimal(0), total=_dec(r["total"]) or Decimal(0),
            parts=r["parts"], priced=r["priced"], unpriced=r["unpriced"], cart_key=r["cart_key"],
            queried_at=_date(r["queried_at"]))

    def _query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        try:
            with closing(self._connect()) as conn:
                return conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"No se pudo leer el historial: {exc}") from exc

    def entries(self, text: str = "", limit: int = 2000) -> list[HistoryEntry]:
        """Cotizaciones guardadas, de la más nueva a la más antigua (filtra por BOM, cliente o carro)."""
        sql = f"SELECT {self._ENTRY_COLUMNS} FROM quotes"
        args: list = []
        if text.strip():
            like = f"%{text.strip()}%"
            sql += " WHERE bom_name LIKE ? OR client LIKE ? OR cart_key LIKE ?"
            args += [like, like, like]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        return [self._entry(r) for r in self._query(sql, args)]

    def entry(self, entry_id: int) -> HistoryEntry | None:
        rows = self._query(f"SELECT {self._ENTRY_COLUMNS} FROM quotes WHERE id = ?", (int(entry_id),))
        return self._entry(rows[0]) if rows else None

    def load(self, entry_id: int) -> Snapshot:
        rows = self._query("SELECT snapshot FROM quotes WHERE id = ?", (int(entry_id),))
        if not rows:
            raise HistoryError(f"No existe la cotización N° {entry_id} en el historial.")
        try:
            data = json.loads(zlib.decompress(rows[0]["snapshot"]).decode("utf-8"))
            return snapshot_from_json(data)
        except HistoryError:
            raise
        except (zlib.error, ValueError, TypeError, KeyError) as exc:
            raise HistoryError(f"La cotización N° {entry_id} está dañada: {exc}") from exc

    def delete(self, entry_id: int) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM quotes WHERE id = ?", (int(entry_id),))
        except sqlite3.Error as exc:
            raise HistoryError(f"No se pudo eliminar del historial: {exc}") from exc

    def part_history(self, mouser_pn: str = "", mpn: str = "", limit: int = 200) -> list[PriceRecord]:
        """Precios guardados de una parte (por código Mouser, o por MPN si no hay código), del más nuevo
        al más antiguo, sin repetir la misma consulta guardada dos veces."""
        if normalize_pn(mouser_pn):
            where, key = "p.mouser_key = ?", normalize_pn(mouser_pn)
        elif normalize_pn(mpn):
            where, key = "p.mpn_key = ?", normalize_pn(mpn)
        else:
            return []
        sql = ("SELECT p.*, q.bom_name, q.client FROM prices p JOIN quotes q ON q.id = p.quote_id "
               f"WHERE {where} ORDER BY COALESCE(p.queried_at, p.created_at) DESC, p.id DESC LIMIT ?")
        rows = self._query(sql, (key, int(limit) * 3))
        records: list[PriceRecord] = []
        seen: set[tuple] = set()
        for r in rows:
            signature = (r["mouser_key"], r["queried_at"] or r["created_at"], r["buy_qty"], r["unit_price"], r["stock"])
            if signature in seen:
                continue
            seen.add(signature)
            try:
                breaks = [(int(q), Decimal(str(p))) for q, p in json.loads(r["breaks"] or "[]")]
            except (ValueError, TypeError, InvalidOperation):
                breaks = []
            records.append(PriceRecord(
                quote_id=r["quote_id"], created_at=_date(r["created_at"]) or datetime.min,
                queried_at=_date(r["queried_at"]), bom_name=r["bom_name"], client=r["client"],
                mouser_pn=r["mouser_pn"], mpn=r["mpn"], manufacturer=r["manufacturer"], stock=r["stock"],
                required=r["required"], buy_qty=r["buy_qty"], unit_price=_dec(r["unit_price"]),
                currency=r["currency"], breaks=breaks))
            if len(records) >= limit:
                break
        return records


# --- Comparación con los precios actuales ----------------------------------------------

@dataclass
class ComparisonRow:
    item_id: int
    label: str
    designators: str
    before_pn: str
    after_pn: str
    before_qty: int
    after_qty: int
    before_unit: Decimal | None
    after_unit: Decimal | None
    before_total: Decimal | None
    after_total: Decimal | None
    before_stock: int | None
    after_stock: int | None
    currency: str = ""

    @property
    def change_pct(self) -> Decimal | None:
        if self.before_total and self.after_total is not None:
            return ((self.after_total - self.before_total) / self.before_total * 100).quantize(Decimal("0.1"))
        return None

    @property
    def status(self) -> str:
        if self.before_total is None and self.after_total is None:
            return "Sin precio"
        if self.after_total is None:
            return "Sin precio ahora"
        if self.before_total is None:
            return "Ahora tiene precio"
        if normalize_pn(self.before_pn) != normalize_pn(self.after_pn):
            return "Cambió la parte elegida"
        if self.after_total > self.before_total:
            return "Subió"
        if self.after_total < self.before_total:
            return "Bajó"
        return "Sin cambio"


def compare_quotes(items: list[BomItem], before: dict[int, ItemQuote], after: list[ItemQuote]) -> list[ComparisonRow]:
    """Compara, línea por línea, la cotización guardada (`before`, por id de ítem) con la actual."""
    rows = []
    for item, now in zip(items, after):
        old = before.get(item.id)
        if old is None or not item.include:
            continue
        old_part, new_part = old.part, now.part
        label = item.mpn or item.mouser_pn or (item.spec.label() if item.spec is not None else "") or item.value
        rows.append(ComparisonRow(
            item_id=item.id, label=label, designators=item.designators,
            before_pn=old_part.mouser_pn if old_part else "", after_pn=new_part.mouser_pn if new_part else "",
            before_qty=old.buy_qty, after_qty=now.buy_qty,
            before_unit=old.unit_price, after_unit=now.unit_price,
            before_total=old.ext_price, after_total=now.ext_price,
            before_stock=old_part.stock if old_part else None, after_stock=new_part.stock if new_part else None,
            currency=now.currency or old.currency))
    return rows
