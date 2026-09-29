"""Configuración persistente del usuario (API key, parámetros y uso diario de la API).

Se guarda en el perfil del usuario, nunca en el repositorio:
- Windows: %APPDATA%\\MouserEngine\\config.json
- macOS:   ~/Library/Application Support/MouserEngine/config.json
- Linux:   ~/.config/MouserEngine/config.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path

from .mouser_api import DAILY_LIMIT

ENV_API_KEY = "MOUSER_API_KEY"
ENV_CART_API_KEY = "MOUSER_CART_API_KEY"
DEFAULT_SCENARIOS = "1, 10, 25, 50, 100, 500, 1000"


def parse_quantities(text: str) -> list[int]:
    """"1, 10, 25; 50 100" -> [1, 10, 25, 50, 100] (sin repetidos, ordenado, solo positivos)."""
    values = set()
    for token in re.split(r"[^\d.]+", text or ""):
        token = token.strip(".")
        if not token:
            continue
        try:
            number = int(float(token))
        except ValueError:
            continue
        if 0 < number <= 10_000_000:
            values.add(number)
    return sorted(values)


def config_dir() -> Path:
    override = os.environ.get("MOUSER_ENGINE_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "MouserEngine"


@dataclass
class Settings:
    api_key: str = ""
    cart_api_key: str = ""
    company_name: str = "FARADIUM SPA"
    client_name: str = ""
    scenario_quantities: str = DEFAULT_SCENARIOS
    scenario_max: int = 1000
    chart_mode: str = "overlay"  # overlay (superpuesto) | split (dos gráficos)
    report_page_size: str = "letter"  # informe PDF: letter (carta) | a4
    passives_enabled: bool = True
    res_tolerance_default: float = 5.0
    cap_tolerance_default: float = 20.0
    cap_voltage_default: float = 16.0
    batch_size: int = 10
    timeout: int = 30
    auto_refresh_minutes: int = 0
    auto_query_on_load: bool = True
    fuzzy_search: bool = True
    last_dir: str = ""
    boards: int = 1
    spares_pct: float = 0.0
    passive_spares_pct: float = 0.0
    optimize_breaks: bool = False
    landed_cost: bool = False  # precio con todo incluido (puesto en Chile)
    import_rates: dict = field(default_factory=dict)  # último dólar observado y aduanero obtenidos
    import_rules: dict = field(default_factory=dict)  # últimas reglas de importación descargadas
    usage_date: str = ""
    usage_count: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    # --- API key ----------------------------------------------------------

    @property
    def effective_api_key(self) -> str:
        return (os.environ.get(ENV_API_KEY) or self.api_key or "").strip()

    @property
    def api_key_from_env(self) -> bool:
        return bool(os.environ.get(ENV_API_KEY, "").strip())

    @property
    def effective_cart_api_key(self) -> str:
        return (os.environ.get(ENV_CART_API_KEY) or self.cart_api_key or "").strip()

    @property
    def scenario_list(self) -> list[int]:
        return parse_quantities(self.scenario_quantities) or parse_quantities(DEFAULT_SCENARIOS)

    # --- Contador diario de consultas -------------------------------------

    def register_call(self) -> None:
        """Cuenta una consulta a la API (se llama desde el hilo de consulta)."""
        with self._lock:
            today = date.today().isoformat()
            if self.usage_date != today:
                self.usage_date = today
                self.usage_count = 0
            self.usage_count += 1

    @property
    def calls_today(self) -> int:
        with self._lock:
            return self.usage_count if self.usage_date == date.today().isoformat() else 0

    @property
    def calls_left(self) -> int:
        return max(0, DAILY_LIMIT - self.calls_today)

    # --- Persistencia -----------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_dir() / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        values = {}
        for key, value in data.items():
            if key not in known:
                continue
            default = getattr(cls(), key)
            try:
                values[key] = type(default)(value) if not isinstance(default, bool) else bool(value)
            except (TypeError, ValueError):
                continue
        return cls(**values)

    def save(self, path: Path | None = None) -> None:
        path = path or config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
