"""Genera capturas de la interfaz usando el servidor simulado de Mouser (sin red).

Uso: python tools/screenshots.py [carpeta_salida]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_mouser import FakeMouserServer  # noqa: E402

from mouser_engine.app import create_app  # noqa: E402
from mouser_engine.bom import load_table  # noqa: E402
from mouser_engine.cart import cart_lines  # noqa: E402
from mouser_engine.config import Settings  # noqa: E402
from mouser_engine.gui.cart_dialogs import CartConfirmDialog, CartResultDialog  # noqa: E402
from mouser_engine.gui.dialogs import ImportDialog, SearchDialog, SettingsDialog  # noqa: E402
from mouser_engine.gui.history_dialogs import ComparisonDialog, HistoryDialog  # noqa: E402
from mouser_engine.gui.main_window import MainWindow  # noqa: E402
from mouser_engine.gui.report_dialog import ClientReportDialog  # noqa: E402
from mouser_engine.mouser_api import MouserClient, RateLimiter  # noqa: E402


def pump(app, seconds: float = 0.3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "capturas")
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MOUSER_ENGINE_CONFIG_DIR", str(out / "_config"))
    app = create_app(sys.argv)
    server = FakeMouserServer()

    def factory(key):
        return MouserClient(key, opener=server.opener, rate_limiter=RateLimiter(1000, 60))

    settings = Settings(api_key="test-key", cart_api_key="cart-key", boards=10, passive_spares_pct=10,
                        client_name="Cliente de ejemplo")
    window = MainWindow(settings=settings, client_factory=factory, persist=False)
    window.interactive = False
    window.resize(1500, 920)
    window.show()
    pump(app, 0.8)
    window.grab().save(str(out / "01_inicio.png"))

    table = load_table(ROOT / "ejemplos" / "bom_ejemplo.csv")
    dialog = ImportDialog(table, window)
    dialog.show()
    pump(app)
    dialog.grab().save(str(out / "02_importar.png"))
    dialog.close()

    window.load_table(dialog.table)
    deadline = time.monotonic() + 20
    while window._lookup_worker is not None and time.monotonic() < deadline:
        pump(app, 0.05)
    pump(app, 0.5)
    lm358 = next(i for i in window.items if i.mpn == "LM358DR")
    window.select_item(lm358)
    pump(app)
    window.grab().save(str(out / "03_cotizacion.png"))

    diode = next(i for i in window.items if i.mpn == "1N4148")
    window.select_item(diode)
    window.detail.setCurrentIndex(2)
    pump(app)
    window.grab().save(str(out / "04_opciones.png"))

    cap = next(i for i in window.items if i.mpn == "GRM188R71C104KA01D")
    window.select_item(cap)
    window.detail.setCurrentIndex(1)
    window.boards_spin.setValue(250)
    window.vat_spin.setValue(19)
    window.fx_spin.setValue(950)
    window.optimize_check.setChecked(True)
    pump(app, 0.6)
    window.grab().save(str(out / "05_tramos_optimizados.png"))

    window.boards_spin.setValue(10)
    window.vat_spin.setValue(0)
    window.fx_spin.setValue(0)
    window.optimize_check.setChecked(False)
    window.main_tabs.setCurrentIndex(1)
    deadline = time.monotonic() + 10
    while not window.scenarios.chart.points and time.monotonic() < deadline:
        pump(app, 0.05)
    pump(app, 0.6)
    window.scenarios.set_boards(40)
    pump(app)
    window.grab().save(str(out / "08_escenarios.png"))
    window.scenarios.mode_combo.setCurrentIndex(1)
    window.scenarios.set_boards(300)
    pump(app)
    window.grab().save(str(out / "09_escenarios_separados.png"))
    window.scenarios.mode_combo.setCurrentIndex(0)
    window.main_tabs.setCurrentIndex(0)

    passive = next(i for i in window.items if i.by_spec)
    window.select_item(passive)
    window.detail.setCurrentIndex(2)
    pump(app)
    window.grab().save(str(out / "10_pasivo_automatico.png"))

    # Historial: dos cotizaciones guardadas con precios distintos y la comparación con "hoy".
    window.boards_spin.setValue(10)
    pump(app)
    first_id = window.save_to_history()
    cap_raw = next(p for p in server.parts if p["MouserPartNumber"] == "81-GRM188R71C104KA1D")
    cap_raw["PriceBreaks"][1]["Price"] = "$0.024"
    window.start_lookup()
    deadline = time.monotonic() + 20
    while window._lookup_worker is not None and time.monotonic() < deadline:
        pump(app, 0.05)
    window.export_excel(str(out / "_config" / "cotizacion.xlsx"))
    history = HistoryDialog(window.history, window)
    history.show()
    pump(app)
    history.grab().save(str(out / "11_historial.png"))
    history.close()

    cap_raw["PriceBreaks"][1]["Price"] = "$0.030"
    window.open_history_entry(first_id, compare=True)
    deadline = time.monotonic() + 20
    while window.last_comparison is None and time.monotonic() < deadline:
        pump(app, 0.05)
    comparison = ComparisonDialog(window.last_comparison, window._compare_before_total, window.summary.total,
                                  window.summary.currency, window.history_entry.created_at, window)
    comparison.show()
    pump(app)
    comparison.grab().save(str(out / "12_comparacion.png"))
    comparison.close()

    cap = next(i for i in window.items if i.mpn == "GRM188R71C104KA01D")
    window.select_item(cap)
    window.detail.setCurrentWidget(window.detail.history_page)
    pump(app)
    window.grab().save(str(out / "13_historial_precios.png"))

    lines = cart_lines(window.items, window.quotes)
    confirm = CartConfirmDialog(lines, window.summary.currency, window._cart_notes(lines), window)
    confirm.show()
    pump(app)
    confirm.grab().save(str(out / "14_carro_confirmar.png"))
    confirm.close()
    result = window.cart_client().cart_insert(lines)
    local_total = sum(line.ext_price for line in lines if line.ext_price is not None)
    cart_dialog = CartResultDialog(result, local_total, window)
    cart_dialog.show()
    pump(app)
    cart_dialog.grab().save(str(out / "15_carro_resultado.png"))
    cart_dialog.close()

    report_dialog = ClientReportDialog(window._report_options(), window)
    report_dialog.show()
    pump(app)
    report_dialog.grab().save(str(out / "16_informe_opciones.png"))
    report_dialog.close()
    window.export_client_report(str(out / "17_informe_cliente.pdf"))

    settings_dialog = SettingsDialog(window.settings, window.client, window.workers, window)
    settings_dialog.show()
    pump(app)
    settings_dialog.grab().save(str(out / "06_configuracion.png"))
    settings_dialog.close()

    resistor = next(i for i in window.items if not i.mpn and not i.mouser_pn and i.qty_per_board)
    search = SearchDialog(window.client(), window.workers, resistor, 110, "4.7K 0603", window)
    search.show()
    deadline = time.monotonic() + 10
    while not search.parts and time.monotonic() < deadline:
        pump(app, 0.05)
    pump(app, 0.3)
    search.grab().save(str(out / "07_buscar.png"))
    search.close()

    window.close()
    print(f"Capturas en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
