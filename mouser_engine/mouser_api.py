"""Cliente de la Mouser Search API (v1).

Documentación oficial: https://api.mouser.com/api/docs/ui/index

Se usa solo la biblioteca estándar (urllib) para que la aplicación respete el almacén de
certificados y el proxy configurados en Windows, sin dependencias adicionales.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import __version__
from .models import Part
from .utils import chunks, normalize_pn

API_BASE = "https://api.mouser.com/api/v1"
MAX_PARTS_PER_QUERY = 10  # la API acepta hasta 10 números de parte separados por "|"
DAILY_LIMIT = 1000  # límite diario de consultas informado por Mouser para la Search API


class MouserError(Exception):
    """Error general al consultar Mouser."""


class MouserAuthError(MouserError):
    """API key inválida o sin permisos."""


class MouserConnectionError(MouserError):
    """No se pudo conectar con api.mouser.com."""


class MouserRateLimitError(MouserError):
    """Se superó el límite de consultas de la API."""


class MouserCancelled(MouserError):
    """El usuario canceló la consulta."""


class RateLimiter:
    """Ventana deslizante: como máximo `max_calls` consultas cada `period` segundos."""

    def __init__(self, max_calls: int = 28, period: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_calls = max(1, max_calls)
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self, cancel: threading.Event | None = None) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                delay = self.period - (now - self._calls[0]) + 0.05
            _sleep_cancellable(self._sleep, delay, cancel)


def _sleep_cancellable(sleep: Callable[[float], None], seconds: float,
                       cancel: threading.Event | None) -> None:
    """Espera `seconds` en tramos cortos para poder cancelar a mitad de camino."""
    remaining = seconds
    while remaining > 0:
        if cancel is not None and cancel.is_set():
            raise MouserCancelled("Consulta cancelada.")
        step = min(0.5, remaining)
        sleep(step)
        remaining -= step
    if cancel is not None and cancel.is_set():
        raise MouserCancelled("Consulta cancelada.")


@dataclass
class LookupResult:
    """Resultado de buscar un número de parte (MPN o código Mouser)."""

    query: str
    exact: list[Part] = field(default_factory=list)
    near: list[Part] = field(default_factory=list)
    suggestions: list[Part] = field(default_factory=list)
    error: str = ""


def part_matches(query: str, part: Part) -> bool:
    key = normalize_pn(query)
    return bool(key) and key in (normalize_pn(part.mpn), normalize_pn(part.mouser_pn))


class MouserClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        on_request: Callable[[], None] | None = None,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter()
        self.max_retries = max_retries
        self.on_request = on_request
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    # --- HTTP -------------------------------------------------------------

    def _post(self, path: str, body: dict, cancel: threading.Event | None = None) -> dict:
        if not self.api_key:
            raise MouserAuthError("No hay API key configurada. Ingrésela en Configuración.")
        url = f"{API_BASE}/{path}?apiKey={urllib.parse.quote(self.api_key)}"
        data = json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise MouserCancelled("Consulta cancelada.")
            self.rate_limiter.wait(cancel)
            if self.on_request:
                self.on_request()
            request = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"MouserEngine/{__version__}",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    payload = response.read()
                result = json.loads(payload.decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                detail = _read_error_body(exc)
                if exc.code in (401, 403) and not _looks_like_rate_limit(detail):
                    raise MouserAuthError(_auth_message(detail)) from exc
                if exc.code == 429 or _looks_like_rate_limit(detail):
                    if _looks_like_daily_limit(detail):
                        raise MouserRateLimitError(
                            "Se alcanzó el límite diario de consultas de la API de Mouser. "
                            "Intente nuevamente mañana.") from exc
                    if attempt < self.max_retries:
                        attempt += 1
                        _sleep_cancellable(self._sleep, 60.0, cancel)
                        continue
                    raise MouserRateLimitError(
                        "Mouser rechazó la consulta por exceso de solicitudes por minuto.") from exc
                if exc.code >= 500 and attempt < self.max_retries:
                    attempt += 1
                    _sleep_cancellable(self._sleep, 2.0 * attempt, cancel)
                    continue
                raise MouserError(f"Mouser respondió HTTP {exc.code}: {detail or exc.reason}") from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                if "tunnel connection failed" in str(reason).lower():
                    raise MouserConnectionError(
                        f"El proxy o firewall de la red bloqueó la conexión a api.mouser.com "
                        f"({reason}). Pida que se permita ese dominio.") from exc
                if attempt < self.max_retries:
                    attempt += 1
                    _sleep_cancellable(self._sleep, 2.0 * attempt, cancel)
                    continue
                raise MouserConnectionError(
                    f"No se pudo conectar con api.mouser.com ({reason}). "
                    "Revise la conexión a internet o el proxy/firewall.") from exc
            except json.JSONDecodeError as exc:
                raise MouserError("Mouser devolvió una respuesta que no es JSON válido.") from exc

            errors = result.get("Errors") or []
            if errors:
                message = "; ".join(
                    str(e.get("Message") or e.get("Code") or e) for e in errors if e
                ) or "Error desconocido"
                if _looks_like_auth_error(errors):
                    raise MouserAuthError(_auth_message(message))
                if _looks_like_rate_limit(message):
                    if _looks_like_daily_limit(message):
                        raise MouserRateLimitError(
                            "Se alcanzó el límite diario de consultas de la API de Mouser.")
                    if attempt < self.max_retries:
                        attempt += 1
                        _sleep_cancellable(self._sleep, 60.0, cancel)
                        continue
                    raise MouserRateLimitError(message)
                raise MouserError(f"Mouser informó un error: {message}")
            return result

    # --- Búsquedas --------------------------------------------------------

    def search_part_numbers(self, part_numbers: list[str], exact: bool = True,
                            cancel: threading.Event | None = None) -> list[Part]:
        """Busca hasta 10 números de parte (MPN o código Mouser) en una sola consulta."""
        cleaned = [pn.replace("|", " ").strip() for pn in part_numbers if pn and pn.strip()]
        if not cleaned:
            return []
        if len(cleaned) > MAX_PARTS_PER_QUERY:
            raise ValueError(f"Máximo {MAX_PARTS_PER_QUERY} números de parte por consulta")
        body = {
            "SearchByPartRequest": {
                "mouserPartNumber": "|".join(cleaned),
                "partSearchOptions": "Exact" if exact else "None",
            }
        }
        result = self._post("search/partnumber", body, cancel)
        return _parts_from(result)

    def search_keyword(self, keyword: str, records: int = 50, in_stock_only: bool = False,
                       starting_record: int = 0,
                       cancel: threading.Event | None = None) -> tuple[int, list[Part]]:
        body = {
            "SearchByKeywordRequest": {
                "keyword": keyword.strip(),
                "records": max(1, min(50, records)),
                "startingRecord": max(0, starting_record),
                "searchOptions": "InStock" if in_stock_only else "None",
                "searchWithYourSignUpLanguage": "false",
            }
        }
        result = self._post("search/keyword", body, cancel)
        total = int((result.get("SearchResults") or {}).get("NumberOfResult") or 0)
        return total, _parts_from(result)

    def test_connection(self) -> str:
        """Hace una consulta mínima para validar la API key. Devuelve un mensaje."""
        parts = self.search_part_numbers(["LM358DR"], exact=True)
        currency = next((p.currency for p in parts if p.currency), "")
        suffix = f" Moneda de la cuenta: {currency}." if currency else ""
        return f"Conexión correcta con la API de Mouser.{suffix}"

    # --- Búsqueda de un BOM completo -------------------------------------

    def lookup(
        self,
        queries: list[str],
        batch_size: int = MAX_PARTS_PER_QUERY,
        fuzzy: bool = True,
        max_fuzzy: int = 40,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, LookupResult]:
        """Busca muchos números de parte agrupándolos en lotes.

        - `exact`: partes cuyo MPN o código Mouser coincide con lo buscado.
        - `near`: lo que Mouser devolvió para una búsqueda exacta individual, pero cuyo MPN
          difiere en la forma de escribirse (se puede usar, con advertencia).
        - `suggestions`: resultados de una búsqueda aproximada cuando no hubo coincidencias.
        """
        unique: list[str] = []
        seen: set[str] = set()
        for q in queries:
            key = normalize_pn(q)
            if key and key not in seen:
                seen.add(key)
                unique.append(q.strip())
        results = {q: LookupResult(q) for q in unique}
        total = len(unique)
        done = 0
        batch_size = max(1, min(MAX_PARTS_PER_QUERY, int(batch_size or MAX_PARTS_PER_QUERY)))

        def report(message: str) -> None:
            if progress:
                progress(done, total, message)

        report("Consultando Mouser…")
        pending_single: list[str] = []
        for batch in chunks(unique, batch_size):
            if cancel is not None and cancel.is_set():
                raise MouserCancelled("Consulta cancelada.")
            try:
                parts = self.search_part_numbers(batch, exact=True, cancel=cancel)
            except (MouserAuthError, MouserConnectionError, MouserRateLimitError, MouserCancelled):
                raise
            except MouserError as exc:
                if len(batch) > 1:
                    pending_single.extend(batch)
                else:
                    results[batch[0]].error = str(exc)
                done += len(batch)
                report(f"{done} de {total} partes consultadas")
                continue
            unmatched = []
            for part in parts:
                owners = [q for q in batch if part_matches(q, part)]
                for q in owners:
                    results[q].exact.append(part)
                if not owners:
                    unmatched.append(part)
            missing = [q for q in batch if not results[q].exact]
            if len(batch) == 1 and missing and parts:
                results[batch[0]].near.extend(parts)
            elif missing and unmatched:
                pending_single.extend(missing)
            done += len(batch)
            report(f"{done} de {total} partes consultadas")

        for q in pending_single:
            if cancel is not None and cancel.is_set():
                raise MouserCancelled("Consulta cancelada.")
            report(f"Revisando {q}…")
            try:
                parts = self.search_part_numbers([q], exact=True, cancel=cancel)
            except (MouserAuthError, MouserConnectionError, MouserRateLimitError, MouserCancelled):
                raise
            except MouserError as exc:
                results[q].error = str(exc)
                continue
            for part in parts:
                (results[q].exact if part_matches(q, part) else results[q].near).append(part)

        if fuzzy:
            not_found = [q for q in unique if not results[q].exact and not results[q].near
                         and not results[q].error][:max(0, max_fuzzy)]
            for index, q in enumerate(not_found, start=1):
                if cancel is not None and cancel.is_set():
                    raise MouserCancelled("Consulta cancelada.")
                report(f"Buscando alternativas para {q} ({index}/{len(not_found)})…")
                try:
                    parts = self.search_part_numbers([q], exact=False, cancel=cancel)
                except (MouserAuthError, MouserConnectionError, MouserRateLimitError, MouserCancelled):
                    raise
                except MouserError:
                    continue
                results[q].suggestions.extend(_rank_suggestions(q, parts)[:15])

        done = total
        report("Consulta terminada")
        return results


def _parts_from(result: dict) -> list[Part]:
    search = result.get("SearchResults") or {}
    return [Part.from_api(raw) for raw in (search.get("Parts") or []) if isinstance(raw, dict)]


def _rank_suggestions(query: str, parts: list[Part]) -> list[Part]:
    key = normalize_pn(query)

    def score(part: Part) -> tuple:
        mpn = normalize_pn(part.mpn)
        common = 0
        for a, b in zip(key, mpn):
            if a != b:
                break
            common += 1
        return (-common, abs(len(mpn) - len(key)), -(part.stock or 0))

    return sorted(parts, key=score)


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - solo es informativo
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:300]
    errors = data.get("Errors") if isinstance(data, dict) else None
    if errors:
        return "; ".join(str(e.get("Message") or e.get("Code") or e) for e in errors if e)
    if isinstance(data, dict) and data.get("Message"):
        return str(data["Message"])
    return raw.strip()[:300]


def _looks_like_auth_error(errors: list) -> bool:
    for e in errors:
        text = " ".join(str(e.get(k, "")) for k in ("Code", "Message", "ResourceKey", "PropertyName")).lower() \
            if isinstance(e, dict) else str(e).lower()
        if any(word in text for word in ("unique identifier", "api key", "apikey", "unauthorized",
                                         "invalididentifier", "not authorized", "forbidden",
                                         "authorization has been denied")):
            return True
    return False


def _looks_like_rate_limit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in ("too many", "toomany", "rate limit", "exceeded",
                                            "maximum calls", "max calls", "quota"))


def _looks_like_daily_limit(text: str) -> bool:
    lowered = (text or "").lower()
    return "day" in lowered or "daily" in lowered or "diari" in lowered


def _auth_message(detail: str) -> str:
    base = ("Mouser rechazó la API key. Para precios y stock se necesita la clave de la "
            "Search API (en My Mouser → APIs es distinta de la clave de Cart/Order API).")
    return f"{base} Detalle: {detail}" if detail else base
