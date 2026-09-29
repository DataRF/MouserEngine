import json
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.history import (
    HistoryError,
    HistoryStore,
    Snapshot,
    compare_quotes,
    snapshot_from_json,
    snapshot_to_json,
)
from mouser_engine.lookup import lookup_all
from mouser_engine.models import Part, PriceBreak, QuoteParams
from mouser_engine.passives import Defaults
from mouser_engine.quote import apply_lookup, queries_for, quote_all, specs_for, summarize


def quoted_snapshot(client, example_bom, **params) -> Snapshot:
    table = load_table(example_bom)
    items, _ = build_items(table)
    p = QuoteParams(**{"boards": 10, "passive_spares_pct": 10, **params})
    specs, required = specs_for(items, p)
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, datetime(2026, 9, 29, 12, 30), spec_results=bundle.specs)
    return Snapshot(items=items, params=p, bom_path=str(example_bom), client="Cliente de prueba",
                    queried_at=datetime(2026, 9, 29, 12, 30), bom_grid=table.grid, bom_sheet=table.sheet,
                    bom_header_row=table.header_row, bom_mapping=dict(table.mapping))


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "historial.sqlite3")


def test_default_location_is_config_dir(tmp_path):
    assert HistoryStore().path == tmp_path / "config" / "historial.sqlite3"  # MOUSER_ENGINE_CONFIG_DIR (conftest)


def test_snapshot_round_trip_keeps_quote(client, example_bom):
    snapshot = quoted_snapshot(client, example_bom, optimize_breaks=True, freight=20)
    diode = next(i for i in snapshot.items if i.mpn == "1N4148")
    diode.manual_part = diode.candidates[-1]
    diode.include = False
    data = json.loads(json.dumps(snapshot_to_json(snapshot)))  # debe ser JSON puro
    restored = snapshot_from_json(data)
    assert restored.params == snapshot.params and restored.client == "Cliente de prueba"
    assert restored.queried_at == datetime(2026, 9, 29, 12, 30)
    assert restored.bom_grid == snapshot.bom_grid and restored.bom_mapping == snapshot.bom_mapping
    before = summarize(snapshot.items, quote_all(snapshot.items, snapshot.params), snapshot.params)
    after = summarize(restored.items, quote_all(restored.items, restored.params), restored.params)
    assert after.total == before.total and after.goods == before.goods and after.priced == before.priced
    restored_diode = next(i for i in restored.items if i.mpn == "1N4148")
    assert restored_diode.manual_part.mouser_pn == diode.manual_part.mouser_pn and not restored_diode.include
    passive = next(i for i in restored.items if i.by_spec)
    assert passive.spec is not None and passive.spec.complete and passive.candidates
    assert passive.spec_report == next(i for i in snapshot.items if i.by_spec).spec_report
    assert [i.designators for i in restored.items] == [i.designators for i in snapshot.items]
    assert [i.looked_up_at for i in restored.items] == [i.looked_up_at for i in snapshot.items]


def test_part_without_raw_is_serialized():
    part = Part("595-X", "X", "TI", price_breaks=[PriceBreak(1, Decimal("0.5"), "USD"),
                                                  PriceBreak(100, Decimal("0.25"), "USD")],
                stock=40, min_qty=1, mult=1, packaging="Reel, Cut Tape")
    from mouser_engine.history import part_to_api
    again = Part.from_api(part_to_api(part))
    assert again.price_breaks[1].price == Decimal("0.25") and again.currency == "USD"
    assert again.stock == 40 and again.packaging == "Reel, Cut Tape"


def test_save_list_load_delete(client, example_bom, store):
    snapshot = quoted_snapshot(client, example_bom)
    first = store.save(snapshot, "manual", when=datetime(2026, 9, 29, 13, 0))
    second = store.save(snapshot, "cart", cart_key="ABC-123", when=datetime(2026, 9, 29, 14, 0))
    entries = store.entries()
    assert [e.id for e in entries] == [second, first]  # la más nueva primero
    newest = entries[0]
    assert newest.reason_label == "Carro creado en Mouser" and newest.cart_key == "ABC-123"
    assert newest.bom_name == "bom_ejemplo.csv" and newest.client == "Cliente de prueba" and newest.boards == 10
    summary = summarize(snapshot.items, quote_all(snapshot.items, snapshot.params), snapshot.params)
    assert newest.total == summary.total and newest.currency == "USD"
    assert [e.id for e in store.entries("abc-1")] == [second]
    assert [e.id for e in store.entries("cliente de")] == [second, first]
    assert store.entries("otro cliente") == []
    assert store.entry(first).reason == "manual" and store.entry(999) is None
    restored = store.load(first)
    assert len(restored.items) == len(snapshot.items)
    store.delete(first)
    assert [e.id for e in store.entries()] == [second]
    with pytest.raises(HistoryError):
        store.load(first)
    with sqlite3.connect(store.path) as conn:  # los precios se borran con la cotización
        assert conn.execute("SELECT COUNT(*) FROM prices WHERE quote_id = ?", (first,)).fetchone()[0] == 0


def stamp(snapshot: Snapshot, when: datetime) -> Snapshot:
    snapshot.queried_at = when
    for item in snapshot.items:
        item.looked_up_at = when
    return snapshot


def test_part_history(client, example_bom, store, server):
    snapshot = stamp(quoted_snapshot(client, example_bom), datetime(2026, 9, 1, 9, 55))
    store.save(snapshot, when=datetime(2026, 9, 1, 10, 0))
    store.save(snapshot, "excel", when=datetime(2026, 9, 1, 10, 5))  # misma consulta: no se repite
    cap = next(p for p in server.parts if p["MouserPartNumber"] == "81-GRM188R71C104KA1D")
    cap["PriceBreaks"][1]["Price"] = "$0.030"
    later = stamp(quoted_snapshot(client, example_bom), datetime(2026, 9, 20, 9, 0))
    store.save(later, when=datetime(2026, 9, 20, 9, 1))
    records = store.part_history(mouser_pn="81-GRM188R71C104KA1D")
    assert [r.when for r in records] == [datetime(2026, 9, 20, 9, 0), datetime(2026, 9, 1, 9, 55)]
    assert records[0].unit_price == Decimal("0.030") and records[1].unit_price == Decimal("0.021")
    assert records[0].buy_qty == 88 and records[0].bom_name == "bom_ejemplo.csv"
    assert (10, Decimal("0.030")) in records[0].breaks
    assert store.part_history(mpn="GRM188R71C104KA01D")[0].unit_price == Decimal("0.030")
    assert store.part_history() == []


def test_corrupted_snapshot(store, client, example_bom):
    entry_id = store.save(quoted_snapshot(client, example_bom))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE quotes SET snapshot = ? WHERE id = ?", (b"no es zlib", entry_id))
    with pytest.raises(HistoryError, match="dañada"):
        store.load(entry_id)


def test_unwritable_location(tmp_path):
    blocker = tmp_path / "archivo"
    blocker.write_text("x")
    with pytest.raises(HistoryError):
        HistoryStore(blocker / "sub" / "historial.sqlite3").entries()


def test_compare_quotes(client, example_bom, server):
    snapshot = quoted_snapshot(client, example_bom)
    items, params = snapshot.items, snapshot.params
    before = {item.id: q for item, q in zip(items, quote_all(items, params))}
    cap = next(p for p in server.parts if p["MouserPartNumber"] == "81-GRM188R71C104KA1D")
    cap["PriceBreaks"][1]["Price"] = "$0.030"  # sube el condensador
    lm358 = next(p for p in server.parts if p["MouserPartNumber"] == "595-LM358DR")
    lm358["PriceBreaks"] = []  # ya no tiene precio
    specs, required = specs_for(items, params)
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    rows = {r.before_pn or r.label: r for r in compare_quotes(items, before, quote_all(items, params))}
    cap_row = rows["81-GRM188R71C104KA1D"]
    assert cap_row.status == "Subió" and cap_row.change_pct > 0 and cap_row.after_unit == Decimal("0.030")
    assert rows["595-LM358DR"].status == "Sin precio ahora"
    assert rows["71-CRCW06034K70FKEA"].status == "Sin cambio"
