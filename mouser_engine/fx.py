"""Tipo de cambio oficial del día (dólar/euro observado del Banco Central de Chile).

Se consulta en mindicador.cl, que publica los indicadores del Banco Central de Chile.
"""

from __future__ import annotations

import json
import urllib.request

_INDICATORS = {"USD": "dolar", "EUR": "euro"}


class FxError(Exception):
    pass


def fetch_clp_rate(currency: str = "USD", timeout: float = 10.0) -> tuple[float, str]:
    """Devuelve (pesos por unidad de `currency`, fecha AAAA-MM-DD)."""
    code = _INDICATORS.get((currency or "USD").upper())
    if not code:
        raise FxError(f"No hay tipo de cambio automático para {currency}; ingréselo manualmente.")
    request = urllib.request.Request(
        f"https://mindicador.cl/api/{code}",
        headers={"Accept": "application/json", "User-Agent": "MouserEngine"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        latest = data["serie"][0]
        return float(latest["valor"]), str(latest.get("fecha", ""))[:10]
    except Exception as exc:  # noqa: BLE001 - cualquier falla se informa igual
        raise FxError(f"No se pudo obtener el tipo de cambio: {exc}") from exc
