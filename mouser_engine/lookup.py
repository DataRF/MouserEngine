"""Consulta completa de un BOM: números de parte (MPN o código Mouser) y pasivos sin MPN.

Los pasivos se resuelven en dos fases para gastar pocas consultas:
1. Búsqueda por palabra clave (solo partes con stock), evaluando cada resultado.
2. Solo para las especificaciones que no quedaron bien cubiertas (menos de dos opciones de
   fabricantes reconocidos con stock suficiente), números de parte construidos de series
   comunes, consultados en lotes de 10 entre todas las especificaciones.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .models import Part
from .mouser_api import (
    MAX_PARTS_PER_QUERY,
    LookupResult,
    MouserAuthError,
    MouserCancelled,
    MouserClient,
    MouserConnectionError,
    MouserError,
    MouserRateLimitError,
)
from .passives import Defaults, PassiveSpec, constructed_candidates, evaluate, is_recognized, search_keywords
from .pricing import purchase_qty
from .utils import chunks, normalize_pn

_FATAL = (MouserAuthError, MouserConnectionError, MouserRateLimitError, MouserCancelled)


@dataclass
class SpecResult:
    """Resultado de buscar una especificación de resistencia o condensador."""

    candidates: list[Part] = field(default_factory=list)
    evaluated: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    constructed: int = 0
    error: str = ""

    def report(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "accepted": len(self.candidates),
            "rejected": dict(self.rejected),
            "assumptions": list(self.assumptions),
            "searches": list(self.searches),
            "constructed": self.constructed,
            "error": self.error,
        }


@dataclass
class LookupBundle:
    parts: dict[str, LookupResult] = field(default_factory=dict)
    specs: dict[str, SpecResult] = field(default_factory=dict)


def _enough(result: SpecResult, required: int) -> bool:
    good = 0
    for part in result.candidates:
        if not part.orderable or part.stock is None:
            continue
        if is_recognized(part.manufacturer) and part.stock >= purchase_qty(max(required, 1), part.min_qty, part.mult):
            good += 1
    return good >= 2


def _accept(spec: PassiveSpec, parts: list[Part], result: SpecResult, defaults: Defaults,
            seen: set[str]) -> None:
    for part in parts:
        key = normalize_pn(part.mouser_pn) or normalize_pn(part.mpn)
        if key in seen:
            continue
        seen.add(key)
        result.evaluated += 1
        evaluation = evaluate(spec, part, defaults)
        if not result.assumptions and evaluation.assumptions:
            result.assumptions = list(evaluation.assumptions)
        if evaluation.ok:
            result.candidates.append(part)
        else:
            result.rejected[evaluation.reason] = result.rejected.get(evaluation.reason, 0) + 1


def resolve_specs(
    client: MouserClient,
    specs: list[PassiveSpec],
    defaults: Defaults,
    required: dict[str, int] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, SpecResult]:
    required = required or {}
    unique: dict[str, PassiveSpec] = {}
    for spec in specs:
        if spec.complete:
            unique.setdefault(spec.key, spec)
    results = {key: SpecResult() for key in unique}
    seen: dict[str, set[str]] = {key: set() for key in unique}
    total = len(unique)

    def report(done: int, message: str) -> None:
        if progress:
            progress(done, total, message)

    # Fase 1: palabras clave
    for index, (key, spec) in enumerate(unique.items(), start=1):
        if cancel is not None and cancel.is_set():
            raise MouserCancelled("Consulta cancelada.")
        result = results[key]
        for keyword in search_keywords(spec)[:2]:
            report(index - 1, f"Buscando «{spec.label()}»…")
            try:
                _, parts = client.search_keyword(keyword, records=50, in_stock_only=True, cancel=cancel)
            except _FATAL:
                raise
            except MouserError as exc:
                result.error = str(exc)
                break
            result.searches.append(keyword)
            _accept(spec, parts, result, defaults, seen[key])
            if _enough(result, required.get(key, 1)):
                break
        report(index, f"{index} de {total} especificaciones buscadas")

    # Fase 2: números de parte de series comunes, en lotes compartidos
    wanted: dict[str, list[str]] = {}
    for key, spec in unique.items():
        if _enough(results[key], required.get(key, 1)):
            continue
        for pn in constructed_candidates(spec):
            wanted.setdefault(normalize_pn(pn), [pn, []])[1].append(key)
            results[key].constructed += 1
    if wanted:
        pns = [value[0] for value in wanted.values()]
        batches = list(chunks(pns, MAX_PARTS_PER_QUERY))
        for number, batch in enumerate(batches, start=1):
            if cancel is not None and cancel.is_set():
                raise MouserCancelled("Consulta cancelada.")
            report(total, f"Revisando series comunes ({number} de {len(batches)})…")
            try:
                parts = client.search_part_numbers(batch, exact=True, cancel=cancel)
            except _FATAL:
                raise
            except MouserError:
                continue
            by_key: dict[str, list[Part]] = {}
            for part in parts:
                for candidate_key in (normalize_pn(part.mpn), normalize_pn(part.mouser_pn)):
                    if candidate_key in wanted:
                        for spec_key in wanted[candidate_key][1]:
                            by_key.setdefault(spec_key, []).append(part)
            for spec_key, found in by_key.items():
                _accept(unique[spec_key], found, results[spec_key], defaults, seen[spec_key])
    report(total, "Especificaciones resueltas")
    return results


def lookup_all(
    client: MouserClient,
    queries: list[str],
    specs: list[PassiveSpec],
    defaults: Defaults,
    required: dict[str, int] | None = None,
    batch_size: int = MAX_PARTS_PER_QUERY,
    fuzzy: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> LookupBundle:
    """Consulta números de parte y especificaciones, informando un progreso único."""
    n_queries = len({normalize_pn(q) for q in queries if normalize_pn(q)})
    n_specs = len({s.key for s in specs if s.complete})
    total = max(1, n_queries + n_specs)

    def part_progress(done: int, _total: int, message: str) -> None:
        if progress:
            progress(min(done, n_queries), total, message)

    def spec_progress(done: int, _total: int, message: str) -> None:
        if progress:
            progress(n_queries + min(done, n_specs), total, message)

    bundle = LookupBundle()
    if queries:
        bundle.parts = client.lookup(queries, batch_size=batch_size, fuzzy=fuzzy, progress=part_progress,
                                     cancel=cancel)
    if specs:
        bundle.specs = resolve_specs(client, specs, defaults, required, spec_progress, cancel)
    if progress:
        progress(total, total, "Consulta terminada")
    return bundle
