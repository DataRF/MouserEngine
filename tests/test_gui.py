import json
import threading
import time
import urllib.error
from datetime import datetime
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt

from mouser_engine.bom import load_table
from mouser_engine.config import Settings
from mouser_engine.gui.dialogs import ImportDialog, SearchDialog
from mouser_engine.gui.items_model import COL
from mouser_engine.gui.main_window import MainWindow
from mouser_engine.gui.workers import Worker, WorkerPool
from mouser_engine.history import snapshot_from_json
from mouser_engine.landed import ExchangeRates, ImportUpdate
from mouser_engine.lookup import LookupBundle
from mouser_engine.mouser_api import MouserClient, RateLimiter


def today_rates():
    return ExchangeRates(usd=Decimal("942.25"), usd_date="2026-09-29", customs=Decimal("933.9"),
                         customs_date="2026-08-28", customs_month="2026-09",
                         fetched_at=datetime.now().isoformat(timespec="seconds"))


@pytest.fixture
def make_window(qtbot, server):
    def factory(api_key="test-key", opener=None, import_updater=None, **settings):
        settings.setdefault("boards", 10)
        s = Settings(api_key=api_key, **settings)

        def client_factory(key):
            return MouserClient(key, opener=opener or server.opener, rate_limiter=RateLimiter(1000, 60),
                                sleep=lambda x: None, on_request=s.register_call)

        window = MainWindow(settings=s, client_factory=client_factory, persist=False,
                            import_updater=import_updater or (lambda: ImportUpdate(rates=today_rates())))
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
    assert window.summary.priced == 9 and window.summary.currency == "USD"
    passive = next(i for i in window.items if i.rows == [15])
    assert window.quotes[window.items.index(passive)].status == "OK · automática"
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


def test_stale_lookup_results_are_ignored(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    before = window.last_query
    window._lookup_generation = window._items_generation - 1  # resultado de un BOM que ya se reemplazó
    window._lookup_done(LookupBundle())
    assert window.last_query == before


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
    assert window.history.entries()[0].reason == "excel"  # exportar guarda en el historial
    assert window.export_cart(str(tmp_path / "carro.csv"))
    assert (tmp_path / "carro.csv").read_text(encoding="utf-8-sig").startswith("Mouser Part Number")


def test_client_report_from_window(make_window, qtbot, example_bom, tmp_path):
    window = loaded(qtbot, make_window(client_name="ACME"), example_bom)
    assert window.act_report.isEnabled() and window.export_button.isEnabled()
    path = window.export_client_report(str(tmp_path / "informe"))
    assert path == str(tmp_path / "informe.pdf")
    assert (tmp_path / "informe.pdf").read_bytes().startswith(b"%PDF")
    entry = window.history.entries()[0]
    assert entry.reason == "pdf" and entry.client == "ACME"


def test_export_diagnostic_never_includes_keys(make_window, qtbot, example_bom, tmp_path):
    window = loaded(qtbot, make_window(cart_api_key="cart-key"), example_bom)
    assert window.export_diagnostic(str(tmp_path / "diag")) == str(tmp_path / "diag.json")
    text = (tmp_path / "diag.json").read_text(encoding="utf-8")
    assert "test-key" not in text and "cart-key" not in text
    data = json.loads(text)
    assert data["version"] and data["currency"] == "USD" and "api_key" not in data["settings"]
    restored = snapshot_from_json(data["snapshot"])  # permite reproducir el caso con los datos reales
    assert len(restored.items) == len(window.items)
    assert any(item.candidates and item.candidates[0].raw for item in restored.items)


def test_scenarios_tab(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    window.main_tabs.setCurrentWidget(window.scenarios)
    qtbot.waitUntil(lambda: bool(window.scenarios.chart.points), timeout=5000)
    assert window.scenarios.table.rowCount() == 7
    chart = window.scenarios.chart
    assert chart.cursor == 1 and chart.renderer.limit == 1  # parte en 1 placa
    window.scenarios.set_boards(100)
    assert chart.cursor == 100 and chart.cursor_point.boards == 100
    assert "Supone que hay stock" in window.scenarios.note.text()
    window.scenarios.use_quantity.emit(100)  # «Usar en la cotización»
    qtbot.waitUntil(lambda: window.params.boards == 100, timeout=2000)


def test_scenario_chart_grows_with_the_slider(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    window.main_tabs.setCurrentWidget(window.scenarios)
    qtbot.waitUntil(lambda: bool(window.scenarios.chart.points), timeout=5000)
    chart = window.scenarios.chart
    renderer = chart.renderer
    window.scenarios.set_boards(10)
    assert renderer.x_max == pytest.approx(12)  # la cantidad elegida + 20 %
    visible = renderer.visible()
    assert visible[0].boards == 1 and visible[-1].boards == 10  # la curva llega solo hasta la barra
    assert [p.boards for p in visible] == list(range(1, 11))  # cada placa se ve en las primeras cantidades
    rect = renderer.plots(chart.width(), chart.height())[0]
    assert renderer.x_of(10, rect) == pytest.approx(rect.left() + rect.width() / 1.2)
    assert renderer.boards_at(rect.right(), rect) == 12  # un clic a la derecha sigue avanzando
    assert renderer.nearest(12).boards == 10  # la lectura no pasa de la curva dibujada
    window.scenarios.set_boards(400)
    assert renderer.x_max == pytest.approx(480) and renderer.visible()[-1].boards == 400
    chart.grab()  # se dibuja sin errores
    window.scenarios.slider.setValue(0)
    assert chart.cursor == 1 and renderer.visible()[-1].boards == 1
    chart.grab()


def wait_cart(qtbot, window, timeout=10000):
    qtbot.waitUntil(lambda: window._cart_worker is None and not window.workers.busy, timeout=timeout)
    qtbot.wait(50)


def test_create_cart_requires_cart_key(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(), example_bom)
    assert window.act_create_cart.isEnabled()
    assert not window.create_cart()  # sin clave de Cart API no llama a Mouser
    assert not any("/cart" in call["path"] for call in server.calls)


def test_create_cart_and_save_history(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(cart_api_key="cart-key", client_name="ACME"), example_bom)
    assert window.create_cart()
    assert not window.act_create_cart.isEnabled()  # no se puede crear dos veces a la vez
    wait_cart(qtbot, window)
    result = window.last_cart
    assert result is not None and result.cart_key and not result.errors
    assert result.cart_key in window.statusBar().currentMessage()
    assert not server.orders  # nunca se envía un pedido
    entry = window.history.entries()[0]
    assert entry.reason == "cart" and entry.cart_key == result.cart_key and entry.client == "ACME"
    assert window.act_create_cart.isEnabled()


def test_create_cart_with_rejected_key(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(cart_api_key="clave-pendiente"), example_bom)
    assert window.create_cart()
    wait_cart(qtbot, window)
    assert window.last_cart is None
    assert "Cart API" in window.statusBar().currentMessage()
    assert window.history.entries() == []


def test_save_open_and_compare_history(make_window, qtbot, example_bom, server):
    window = loaded(qtbot, make_window(client_name="ACME"), example_bom)
    total = window.summary.total
    entry_id = window.save_to_history()
    assert entry_id and "historial" in window.statusBar().currentMessage()
    cap = next(p for p in server.parts if p["MouserPartNumber"] == "81-GRM188R71C104KA1D")
    cap["PriceBreaks"][1]["Price"] = "$0.030"  # sube el precio en Mouser
    window.boards_spin.setValue(20)
    window.client_edit.setText("Otro")
    qtbot.waitUntil(lambda: window.params.boards == 20, timeout=2000)
    calls = len(server.calls)

    assert window.open_history_entry(entry_id)  # se abre con los precios guardados, sin consultar
    assert window.params.boards == 10 and window.summary.total == total
    assert window.client_edit.text() == "ACME" and window.banner.isVisible()
    assert "historial" in window.windowTitle() and len(server.calls) == calls

    assert window.open_history_entry(entry_id, compare=True)
    wait_idle(qtbot, window)
    assert len(server.calls) > calls
    rows = {r.before_pn: r for r in window.last_comparison}
    assert rows["81-GRM188R71C104KA1D"].status == "Subió"
    assert window.summary.total > total


def test_export_history_entry(make_window, qtbot, example_bom, tmp_path):
    window = loaded(qtbot, make_window(), example_bom)
    entry_id = window.save_to_history()
    assert window.export_history_entry(entry_id, str(tmp_path / "hist")) == str(tmp_path / "hist.xlsx")
    assert (tmp_path / "hist.xlsx").exists()
    assert len(window.history.entries()) == 1  # exportar desde el historial no lo duplica


def test_price_history_tab(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    cap = item_by_mpn(window, "GRM188R71C104KA01D")
    window.select_item(cap)
    window.detail.setCurrentWidget(window.detail.history_page)
    assert window.detail.history_table.rowCount() == 0
    assert "todavía no aparece" in window.detail.history_hint.text()
    window.save_to_history()
    assert window.detail.history_table.rowCount() == 1
    assert "0.021" in window.detail.history_table.item(0, 3).text().replace(",", ".")


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
    row = next(i for i, p in enumerate(dialog.parts) if p.mouser_pn == "603-RC0603FR-074K7L")
    dialog.table.selectRow(row)
    dialog._accept_selected()
    assert dialog.selected_part.mouser_pn == "603-RC0603FR-074K7L"


def test_landed_cost_checkbox(make_window, qtbot, example_bom):
    window = loaded(qtbot, make_window(), example_bom)
    assert not window.landed_check.isChecked() and window.summary.landed is None
    assert window.card_total.title.text() == "Total estimado" and "solo Mouser" in window.card_total.sub.text()
    assert "942,25" in window.import_label.text() and "septiembre" in window.import_label.text()
    goods = window.summary.goods
    window.landed_check.setChecked(True)
    qtbot.waitUntil(lambda: window.summary.landed is not None, timeout=2000)
    cost = window.summary.landed
    assert window.summary.total == cost.total > goods and cost.usd_rate == Decimal("942.25")
    assert cost.customs_rate == Decimal("933.9")
    assert window.card_total.title.text() == "Total puesto en Chile"
    assert "desaduanamiento" in window.card_total.sub.text()
    window.save_settings()
    assert window.settings.landed_cost  # se recuerda para la próxima vez
    dialog = window.show_landed_detail()
    html = dialog.browser.toPlainText()
    assert "Honorario de desaduanamiento" in html and "Total con todo incluido" in html
    assert "dólar aduanero 933,90 CLP de septiembre" in html
    # los escenarios también quedan puestos en Chile
    window.main_tabs.setCurrentWidget(window.scenarios)
    qtbot.waitUntil(lambda: bool(window.scenarios.chart.points), timeout=5000)
    assert "puestos en Chile" in window.scenarios.note.text()


def test_import_data_offline_uses_saved_rates(make_window, qtbot, example_bom):
    saved = today_rates().to_json() | {"fetched_at": "2026-09-28T09:00:00"}
    window = loaded(qtbot, make_window(landed_cost=True, import_rates=saved,
                                       import_updater=lambda: ImportUpdate(rates_error="sin red")), example_bom)
    assert window.summary.landed is not None and window.summary.landed.usd_rate == Decimal("942.25")
    assert "Sin conexión" in window.import_label.text() and "Reintentar" in window.import_label.text()
    never = loaded(qtbot, make_window(landed_cost=True, import_updater=lambda: ImportUpdate(rates_error="x")),
                   example_bom)
    assert never.summary.landed.setup.estimated_rates  # dólar de referencia hasta que haya conexión
    assert "dólar de referencia" in never.import_label.text()
