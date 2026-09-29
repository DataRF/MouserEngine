import threading
import time
import urllib.error

import pytest
from PySide6.QtCore import Qt

from mouser_engine.bom import load_table
from mouser_engine.config import Settings
from mouser_engine.gui.dialogs import ImportDialog, SearchDialog
from mouser_engine.gui.items_model import COL
from mouser_engine.gui.main_window import MainWindow
from mouser_engine.gui.workers import Worker, WorkerPool
from mouser_engine.mouser_api import MouserClient, RateLimiter


@pytest.fixture
def make_window(qtbot, server):
    def factory(api_key="test-key", opener=None, **settings):
        settings.setdefault("boards", 10)
        s = Settings(api_key=api_key, **settings)

        def client_factory(key):
            return MouserClient(key, opener=opener or server.opener, rate_limiter=RateLimiter(1000, 60),
                                sleep=lambda x: None, on_request=s.register_call)

        window = MainWindow(settings=s, client_factory=client_factory, persist=False)
        window.interactive = False
        qtbot.addWidget(window)
        window.show()
        return window
    return factory


def wait_idle(qtbot, window, timeout=10000):
    qtbot.waitUntil(lambda: window._lookup_worker is None and not window.workers.busy, timeout=timeout)
    qtbot.wait(50)


def loaded(qtbot, window, example_bom):
    assert window.load_table(load_table(example_bom))
    wait_idle(qtbot, window)
    return window


def item_by_mpn(window, mpn):
    return next(i for i in window.items if i.mpn == mpn and i.include)


def test_startup_without_key_shows_banner(make_window, qtbot):
    window = make_window(api_key="")
    qtbot.wait(50)
    assert window.banner.isVisible()
    assert "API key" in window.banner.text()
    assert "Sin API key" in window.connection_label.text()
    assert window.stack.currentIndex() == 0


def test_startup_checks_connection(make_window, qtbot):
    window = make_window()
    qtbot.waitUntil(lambda: "Conexión correcta" in window.connection_label.text(), timeout=5000)


def test_load_and_lookup(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(), example_bom)
    assert window.stack.currentIndex() == 1
    assert window.model.rowCount() == 12
    assert window.summary.priced == 8 and window.summary.currency == "USD"
    assert "USD" in window.card_goods.value.text()
    assert window.last_query is not None
    assert window.settings.calls_today == len(server.calls)
    assert window._connection_state[0] == "ok"


def test_recalculates_in_real_time_without_api_calls(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(), example_bom)
    calls = len(server.calls)
    before = window.summary.goods
    window.boards_spin.setValue(100)
    qtbot.waitUntil(lambda: window.params.boards == 100, timeout=2000)
    cap = item_by_mpn(window, "GRM188R71C104KA01D")
    quote = window.quotes[window.items.index(cap)]
    assert quote.required == 800
    assert window.summary.goods > before
    assert len(server.calls) == calls


def test_edit_quantity_and_include(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    stm = item_by_mpn(window, "STM32F103C8T6")
    row = window.items.index(stm)
    assert window.model.setData(window.model.index(row, COL["qty_per_board"]), "3")
    qtbot.waitUntil(lambda: window.quotes[row].required == 30, timeout=2000)
    total_before = window.summary.goods
    window.model.setData(window.model.index(row, COL["include"]), Qt.Unchecked, Qt.CheckStateRole)
    qtbot.waitUntil(lambda: window.quotes[row].level == "excluded", timeout=2000)
    assert window.summary.goods < total_before


def test_edit_mpn_queries_only_that_part(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(), example_bom)
    resistor = next(i for i in window.items if i.rows == [15])
    row = window.items.index(resistor)
    calls = len(server.calls)
    window.model.setData(window.model.index(row, COL["mpn"]), "RC0603FR-074K7L")
    wait_idle(qtbot, window)
    assert len(server.calls) == calls + 1
    assert window.quotes[row].part.mouser_pn == "603-RC0603FR-074K7L"
    assert window.quotes[row].level == "ok"


def test_choose_option_and_back_to_auto(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    diode = item_by_mpn(window, "1N4148")
    window.select_item(diode)
    row = window.items.index(diode)
    assert window.quotes[row].part.manufacturer == "Vishay Semiconductors"
    onsemi = next(p for p in diode.candidates if p.manufacturer == "onsemi")
    window.detail.use_option.emit(onsemi)
    assert window.quotes[row].part is onsemi
    assert "manualmente" in " ".join(window.quotes[row].notes)
    window.detail.auto_select.emit()
    assert window.quotes[row].part.manufacturer == "Vishay Semiconductors"


def test_auth_error_shows_banner(make_window, qtbot, example_bom):
    window = make_window(api_key="clave-mala")
    loaded(qtbot, window, example_bom)
    assert window.banner.isVisible()
    assert "Search API" in window.banner.text()
    assert "rechazada" in window.connection_label.text()


def test_connection_error_keeps_last_prices(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(), example_bom)
    goods = window.summary.goods
    server.fail_next = [urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))]
    assert window.start_lookup()
    wait_idle(qtbot, window)
    assert window.banner.isVisible() and "últimos precios" in window.banner.text()
    assert window.summary.goods == goods


def test_cancel_lookup(make_window, qtbot, example_bom, server):
    started = threading.Event()

    def slow_opener(request, timeout=None):
        started.set()
        time.sleep(0.3)
        return server.opener(request, timeout)

    window = make_window(opener=slow_opener)
    window.load_table(load_table(example_bom))
    qtbot.waitUntil(started.is_set, timeout=3000)
    window.cancel_lookup()
    wait_idle(qtbot, window)
    assert "cancelada" in window.statusBar().currentMessage().lower()
    assert all(i.lookup_state == "pending" for i in window.items if i.base_query)


def test_filters(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    window.filter_edit.setText("lm358")
    assert window.proxy.rowCount() == 1
    window.filter_edit.clear()
    window.status_filter.setCurrentIndex(2)  # Solo errores
    assert window.proxy.rowCount() == window.summary.error
    window.status_filter.setCurrentIndex(0)
    assert window.proxy.rowCount() == 12


def test_export_from_window(make_window, qtbot, example_bom, tmp_path):
    window = loaded(qtbot, make_window(), example_bom)
    assert window.export_excel(str(tmp_path / "cot")) == str(tmp_path / "cot.xlsx")
    assert (tmp_path / "cot.xlsx").exists()
    assert window.export_cart(str(tmp_path / "carro.csv"))
    assert (tmp_path / "carro.csv").read_text(encoding="utf-8-sig").startswith("Mouser Part Number")


def test_worker_signals_arrive_on_main_thread(qtbot):
    main = threading.get_ident()
    seen = {}
    pool = WorkerPool()
    worker = Worker(threading.get_ident)
    worker.signals.result.connect(lambda ident: seen.update(worker=ident, handler=threading.get_ident()))
    pool.start(worker)
    qtbot.waitUntil(lambda: "handler" in seen, timeout=3000)
    assert seen["worker"] != main
    assert seen["handler"] == main


def test_import_dialog_mapping(qtbot, example_bom):
    dialog = ImportDialog(load_table(example_bom))
    qtbot.addWidget(dialog)
    assert "12" in dialog.summary_label.text()
    dialog.combos["mpn"].setCurrentIndex(0)
    assert "mpn" not in dialog.table.mapping
    assert "número de parte" in dialog.summary_label.text()
    dialog.header_spin.setValue(1)
    assert dialog.table.mapping.get("mpn") is None
    dialog.header_spin.setValue(4)
    assert dialog.table.mapping.get("mpn") == 3


def test_search_dialog_assigns_part(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    resistor = next(i for i in window.items if i.rows == [15])
    dialog = SearchDialog(window.client(), window.workers, resistor, 11, "4.7K 0603", window)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: bool(dialog.parts), timeout=3000)
    dialog.table.selectRow(0)
    dialog._accept_selected()
    assert dialog.selected_part.mouser_pn == "603-RC0603FR-074K7L"
