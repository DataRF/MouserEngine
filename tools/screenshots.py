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
from mouser_engine.config import Settings  # noqa: E402
from mouser_engine.gui.dialogs import ImportDialog, SearchDialog, SettingsDialog  # noqa: E402
from mouser_engine.gui.main_window import MainWindow  # noqa: E402
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

    settings = Settings(api_key="test-key", boards=10, passive_spares_pct=10)
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
