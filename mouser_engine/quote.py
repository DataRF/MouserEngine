"""Cotización: elige la mejor opción de Mouser para cada parte y calcula costos."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Iterable

from .models import (
    LEVEL_ERROR,
    LEVEL_EXCLUDED,
    LEVEL_OK,
    LEVEL_ORDER,
    LEVEL_PENDING,
    LEVEL_WARN,
    BomItem,
    ItemQuote,
    Part,
    QuoteParams,
    QuoteSummary,
    lead_time_label,
)
from .formatting import fmt_int, fmt_money
from .mouser_api import LookupResult
from .passives import PassiveSpec, candidate_spec, is_recognized
from .pricing import best_option, money, price_for_qty, purchase_qty
from .utils import mfr_match, normalize_mfr, normalize_pn


def _pct(value: float) -> Decimal:
    return Decimal(str(value or 0)) / Decimal(100)


def required_qty(item: BomItem, params: QuoteParams) -> int:
    """Cantidad necesaria: cantidad por placa × placas + merma (redondeada hacia arriba)."""
    base = max(0, item.qty_per_board) * max(0, int(params.boards))
    pct = params.spares_pct
    if item.is_passive and params.passive_spares_pct:
        pct = params.passive_spares_pct
    extra = (Decimal(base) * _pct(pct)).to_integral_value(rounding=ROUND_CEILING)
    return base + int(max(extra, 0))


def _option_cost(part: Part, required: int) -> Decimal | None:
    qty = purchase_qty(max(required, 1), part.min_qty, part.mult)
    option = price_for_qty(part.price_breaks, qty)
    return option.ext_price if option else None


def rank_options(parts: Iterable[Part], required: int) -> list[Part]:
    """Ordena opciones de mejor a peor: con precio, con stock suficiente, vigente, más barata."""
    def score(part: Part) -> tuple:
        qty = purchase_qty(max(required, 1), part.min_qty, part.mult)
        cost = _option_cost(part, required)
        in_stock = part.stock is not None and part.stock >= qty
        return (
            0 if part.orderable else 1,
            0 if in_stock else 1,
            LEVEL_ORDER.get(part.lifecycle_level, 0),
            cost if cost is not None else Decimal("Infinity"),
            -(part.stock or 0),
        )
    return sorted(parts, key=score)


def rank_spec_options(parts: Iterable[Part], required: int) -> list[Part]:
    """Opciones que ya cumplen la especificación, de mejor a peor: con precio, stock suficiente,
    fabricante reconocido, vigente, menor costo total, mejor tolerancia y más stock."""
    def score(part: Part) -> tuple:
        qty = purchase_qty(max(required, 1), part.min_qty, part.mult)
        cost = _option_cost(part, required)
        in_stock = part.stock is not None and part.stock >= qty
        tolerance = candidate_spec(part).tolerance
        return (
            0 if part.orderable else 1,
            0 if in_stock else 1,
            0 if is_recognized(part.manufacturer) else 1,
            LEVEL_ORDER.get(part.lifecycle_level, 0),
            cost if cost is not None else Decimal("Infinity"),
            tolerance if tolerance is not None else Decimal(100),
            -(part.stock or 0),
        )
    return sorted(parts, key=score)


def select_part(item: BomItem, required: int) -> tuple[Part | None, dict]:
    """Elige la opción a comprar. Devuelve (parte, marcas) con marcas informativas."""
    flags: dict = {}
    if item.manual_part is not None:
        flags["manual"] = True
        return item.manual_part, flags
    if item.by_spec:
        if not item.candidates:
            return None, flags
        best = rank_spec_options(item.candidates, required)[0]
        flags["auto_spec"] = True
        if not is_recognized(best.manufacturer):
            flags["unrecognized"] = True
        return best, flags
    pool = list(item.candidates)
    if not pool and item.near:
        pool = list(item.near)
        flags["near"] = True
    if not pool:
        return None, flags
    if item.manufacturer:
        same = [p for p in pool if mfr_match(item.manufacturer, p.manufacturer)]
        if same:
            pool = same
        else:
            flags["mfr_mismatch"] = True
    elif len({normalize_mfr(p.manufacturer) for p in pool}) > 1:
        flags["ambiguous"] = True
    best = rank_options(pool, required)[0]
    return best, flags


def _spec_summary(report: dict | None) -> str:
    if not report:
        return ""
    evaluated = report.get("evaluated", 0)
    accepted = report.get("accepted", 0)
    text = f"Se evaluaron {fmt_int(evaluated)} opciones de Mouser: {fmt_int(accepted)} cumplen."
    rejected = sorted((report.get("rejected") or {}).items(), key=lambda kv: -kv[1])
    if rejected:
        text += " Descartadas: " + ", ".join(f"{fmt_int(n)} por {reason}" for reason, n in rejected[:6]) + "."
    return text


def _quote_spec_state(item: BomItem, q: ItemQuote) -> bool:
    """Estados previos de una línea por especificación. True si ya no hay nada que elegir."""
    spec: PassiveSpec = item.spec  # type: ignore[assignment]
    if item.manual_part is not None:
        return False
    if not spec.complete:
        q.add(LEVEL_ERROR, "Especificación incompleta",
              f"No se puede elegir automáticamente «{spec.label()}»: falta {', '.join(spec.missing)}. "
              "Complete el BOM o use «Buscar en Mouser».")
        return True
    report = item.spec_report or {}
    if report.get("disabled"):
        q.add(LEVEL_ERROR, "Sin N° de parte",
              "La línea no tiene MPN y la elección automática de pasivos está desactivada "
              "(Configuración → Pasivos sin MPN). Use «Buscar en Mouser».")
        return True
    if item.lookup_state == "pending":
        q.add(LEVEL_PENDING, "Pendiente", f"Se buscará en Mouser por especificación: {spec.label()}.")
        return True
    if item.lookup_state == "error" and not item.candidates:
        q.add(LEVEL_ERROR, "Error de consulta", item.lookup_error or report.get("error") or "Error al consultar Mouser.")
        return True
    if not item.candidates:
        q.add(LEVEL_ERROR, "Sin opción que cumpla",
              f"Ninguna opción en Mouser cumple «{spec.label()}». {_spec_summary(report)} "
              "Revise la especificación o use «Buscar en Mouser».")
        return True
    return False


def quote_item(item: BomItem, params: QuoteParams) -> ItemQuote:
    q = ItemQuote(required=required_qty(item, params))

    if item.by_spec:
        if _quote_spec_state(item, q):
            return _finish(item, q)
    elif item.lookup_state == "noquery" and item.manual_part is None:
        q.add(LEVEL_ERROR, "Sin N° de parte",
              "La línea no tiene MPN ni código Mouser. Use «Buscar en Mouser» para asignar una parte.")
    elif item.lookup_state == "pending" and item.manual_part is None:
        q.add(LEVEL_PENDING, "Pendiente", "Aún no se consulta en Mouser.")
    elif item.lookup_state == "error" and not item.candidates and item.manual_part is None:
        q.add(LEVEL_ERROR, "Error de consulta", item.lookup_error or "Error al consultar Mouser.")

    part, flags = select_part(item, q.required)
    if part is None:
        if item.lookup_state == "done":
            note = f"«{item.base_query}» no se encontró en Mouser."
            if item.suggestions:
                note += f" Hay {len(item.suggestions)} sugerencias en la pestaña «Opciones en Mouser»."
            q.add(LEVEL_ERROR, "No encontrado", note)
        return _finish(item, q)

    q.part = part
    q.auto_selected = not flags.get("manual")
    q.stock = part.stock

    if not part.orderable:
        q.add(LEVEL_ERROR, "Sin precio",
              "Mouser no informa precio para esta parte (puede no estar disponible para venta).")
    elif q.required > 0:
        base, best = best_option(part.price_breaks, q.required, part.min_qty, part.mult, part.stock)
        if base is not None:
            q.base_qty, q.base_unit_price, q.base_ext_price = base.qty, base.unit_price, base.ext_price
            q.opt_qty, q.opt_unit_price, q.opt_ext_price = best.qty, best.unit_price, best.ext_price
            q.savings = max(Decimal(0), base.ext_price - best.ext_price)
            chosen = best if params.optimize_breaks else base
            q.buy_qty, q.unit_price, q.ext_price = chosen.qty, chosen.unit_price, chosen.ext_price
            q.active_break = chosen.price_break
            if base.qty > q.required:
                note = (f"Se deben comprar {fmt_int(base.qty)} (mínimo {fmt_int(part.min_qty)}, "
                        f"múltiplo {fmt_int(part.mult)}) para cubrir {fmt_int(q.required)}.")
                if part.min_qty > 1 and base.qty >= 5 * q.required:
                    q.add(LEVEL_WARN, "Mínimo de compra alto",
                          note + " Revise en «Opciones en Mouser» o busque otro empaque (p. ej. cinta cortada).")
                else:
                    q.notes.append(note)
            if q.savings > 0:
                if params.optimize_breaks:
                    q.notes.append(
                        f"Optimizado por tramo: se compran {fmt_int(best.qty)} en vez de "
                        f"{fmt_int(base.qty)} y se ahorra {fmt_money(q.savings, part.currency)}.")
                else:
                    q.notes.append(
                        f"Comprando {fmt_int(best.qty)} en vez de {fmt_int(base.qty)} el total baja "
                        f"de {fmt_money(base.ext_price, part.currency)} a "
                        f"{fmt_money(best.ext_price, part.currency)}.")

    q.add(LEVEL_OK, "OK · automática" if flags.get("auto_spec") else "OK")
    if flags.get("auto_spec"):
        _explain_auto_choice(item, q, part, flags)

    buy = q.buy_qty or q.required
    if part.stock is None:
        q.add(LEVEL_WARN, "Stock desconocido", "Mouser no informó el stock disponible.")
    elif buy > 0 and part.stock < buy:
        eta = _on_order_text(part)
        lead = f" Plazo de fábrica: {lead_time_label(part.lead_time)}." if part.lead_time else ""
        if part.stock > 0:
            q.add(LEVEL_WARN, "Stock insuficiente",
                  f"Hay {fmt_int(part.stock)} en stock y se necesitan {fmt_int(buy)} "
                  f"(faltan {fmt_int(buy - part.stock)}).{lead}{eta}")
        else:
            q.add(LEVEL_ERROR, "Sin stock", f"Sin stock en Mouser.{lead}{eta}")

    level = part.lifecycle_level
    if level != LEVEL_OK:
        q.add(level, part.lifecycle_label, f"Ciclo de vida en Mouser: {part.lifecycle or part.lifecycle_label}.")
    if part.suggested_replacement:
        q.notes.append(f"Reemplazo sugerido por Mouser: {part.suggested_replacement}.")

    if flags.get("near"):
        q.add(LEVEL_WARN, "Coincidencia aproximada",
              f"El MPN en Mouser ({part.mpn}) no es idéntico al del BOM ({item.base_query}). Verifique.")
    if flags.get("mfr_mismatch"):
        q.add(LEVEL_WARN, "Fabricante distinto",
              f"El BOM indica «{item.manufacturer}» pero Mouser lo vende como «{part.manufacturer}».")
    if flags.get("ambiguous"):
        makers = sorted({p.manufacturer for p in (item.candidates or item.near)})
        q.add(LEVEL_WARN, "Varios fabricantes",
              "El MPN existe para varios fabricantes (" + ", ".join(makers) +
              "). Se eligió la opción más conveniente; confirme el fabricante.")
    if flags.get("manual"):
        q.notes.append("Opción elegida manualmente.")
        if item.base_query and normalize_pn(item.base_query) not in (
                normalize_pn(part.mpn), normalize_pn(part.mouser_pn)):
            q.notes.append(f"Reemplaza a «{item.base_query}» del BOM.")
    if part.restriction:
        q.notes.append(f"Restricción: {part.restriction}")
    for message in part.info_messages:
        q.notes.append(message)
    return _finish(item, q)


def _explain_auto_choice(item: BomItem, q: ItemQuote, part: Part, flags: dict) -> None:
    spec: PassiveSpec = item.spec  # type: ignore[assignment]
    report = item.spec_report or {}
    buy = q.buy_qty or q.required
    with_stock = sum(1 for p in item.candidates
                     if p.stock is not None and p.stock >= purchase_qty(max(buy, 1), p.min_qty, p.mult))
    reasons = ["cumple o supera cada requisito"]
    if not flags.get("unrecognized"):
        reasons.append("fabricante reconocido")
    reasons.append(f"menor costo total con stock suficiente entre {fmt_int(with_stock)} opciones"
                   if with_stock else "no hay opciones con stock suficiente")
    q.notes.append(f"Elegida automáticamente para «{spec.label()}»: {part.mpn} de {part.manufacturer} "
                   f"({'; '.join(reasons)}).")
    summary = _spec_summary(report)
    if summary:
        q.notes.append(summary)
    for note in report.get("assumptions") or []:
        if note.startswith("Tensión no indicada"):
            q.add(LEVEL_WARN, "Revisar tensión", note)
        else:
            q.notes.append(note)
    if flags.get("unrecognized"):
        q.add(LEVEL_WARN, "Fabricante no reconocido",
              f"Ninguna opción de un fabricante reconocido cumple con stock suficiente; se eligió "
              f"{part.manufacturer}. Revíselo en «Opciones en Mouser».")


def _on_order_text(part: Part) -> str:
    if not part.on_order:
        return ""
    entries = ", ".join(
        f"{fmt_int(o.quantity)} para {o.date}" if o.date else fmt_int(o.quantity)
        for o in part.on_order[:3])
    return f" En pedido a fábrica: {entries}."


def _finish(item: BomItem, q: ItemQuote) -> ItemQuote:
    if not item.include:
        q.level = LEVEL_EXCLUDED
        q.status = "Excluido" if item.qty_per_board > 0 else "Cantidad 0"
    return q


def quote_all(items: list[BomItem], params: QuoteParams) -> list[ItemQuote]:
    return [quote_item(item, params) for item in items]


def summarize(items: list[BomItem], quotes: list[ItemQuote], params: QuoteParams) -> QuoteSummary:
    s = QuoteSummary(items=len(items))
    currencies: set[str] = set()
    for item, q in zip(items, quotes):
        if q.level == LEVEL_EXCLUDED:
            s.excluded += 1
            continue
        s.included += 1
        if q.level == LEVEL_OK:
            s.ok += 1
        elif q.level == LEVEL_WARN:
            s.warn += 1
        elif q.level == LEVEL_ERROR:
            s.error += 1
        else:
            s.pending += 1
        if q.ext_price is not None:
            s.priced += 1
            s.subtotal += q.base_ext_price if q.base_ext_price is not None else q.ext_price
            s.optimized_subtotal += q.opt_ext_price if q.opt_ext_price is not None else q.ext_price
            s.goods += q.ext_price
            if q.currency:
                currencies.add(q.currency)
        else:
            s.unpriced += 1
    s.savings = s.subtotal - s.optimized_subtotal
    s.currency = next(iter(currencies)) if len(currencies) == 1 else (
        "/".join(sorted(currencies)) if currencies else "")
    s.mixed_currency = len(currencies) > 1
    s.freight = money(Decimal(str(params.freight or 0)))
    base = s.goods + s.freight
    s.duty = money(base * _pct(params.duty_pct))
    s.vat = money((base + s.duty) * _pct(params.vat_pct))
    s.total = money(base + s.duty + s.vat)
    if params.fx_rate and params.fx_rate > 0 and s.currency != "CLP":  # en CLP no hay nada que convertir
        s.total_clp = (s.total * Decimal(str(params.fx_rate))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return s


def apply_lookup(items: list[BomItem], results: dict[str, LookupResult],
                 when: datetime | None = None, spec_results: dict | None = None) -> None:
    """Guarda en cada BomItem el resultado de la consulta a Mouser."""
    by_key = {normalize_pn(k): v for k, v in results.items()}
    when = when or datetime.now()
    for item in items:
        if item.by_spec and spec_results is not None and item.spec.complete:
            spec_result = spec_results.get(item.spec.key)
            if spec_result is not None:
                item.candidates = list(spec_result.candidates)
                item.near, item.suggestions = [], []
                item.spec_report = spec_result.report()
                item.lookup_error = spec_result.error
                item.lookup_state = "error" if spec_result.error and not spec_result.candidates else "done"
                item.looked_up_at = when
        if item.manual_part is not None:
            fresh = by_key.get(normalize_pn(item.manual_part.mouser_pn))
            if fresh:
                match = next((p for p in fresh.exact + fresh.near
                              if normalize_pn(p.mouser_pn) == normalize_pn(item.manual_part.mouser_pn)), None)
                if match:
                    item.manual_part = match
        if not item.base_query:
            continue
        result = by_key.get(normalize_pn(item.base_query))
        if result is None:
            continue
        item.candidates = list(result.exact)
        item.near = list(result.near)
        item.suggestions = list(result.suggestions)
        item.lookup_error = result.error
        item.lookup_state = "error" if result.error and not result.exact and not result.near else "done"
        item.looked_up_at = when


def specs_for(items: list[BomItem], params: QuoteParams) -> tuple[list[PassiveSpec], dict[str, int]]:
    """Especificaciones a resolver (líneas incluidas, sin MPN ni opción manual) y la cantidad
    requerida de cada una, para saber cuándo hay suficientes opciones con stock."""
    specs: list[PassiveSpec] = []
    required: dict[str, int] = {}
    for item in items:
        if not item.by_spec or item.manual_part is not None or not item.include:
            continue
        spec: PassiveSpec = item.spec  # type: ignore[assignment]
        if not spec.complete:
            continue
        specs.append(spec)
        required[spec.key] = max(required.get(spec.key, 0), required_qty(item, params))
    return specs, required


def mark_specs_disabled(items: list[BomItem]) -> None:
    """Con la elección automática desactivada, las líneas por especificación quedan sin consultar."""
    for item in items:
        if item.by_spec and item.manual_part is None:
            item.spec_report = {"disabled": True}
            item.candidates = []
            item.lookup_state = "done"


def queries_for(items: list[BomItem]) -> list[str]:
    """Números de parte a consultar (incluye las opciones elegidas manualmente)."""
    queries: list[str] = []
    for item in items:
        if item.base_query:
            queries.append(item.base_query)
        if item.manual_part is not None and item.manual_part.mouser_pn:
            queries.append(item.manual_part.mouser_pn)
    return queries
