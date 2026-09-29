"""Líneas del carro de Mouser: se usan para crear el carro con la Cart API y para el CSV.

Reglas de la Cart API (guía de Mouser): solo números de parte de Mouser, hasta 100 ítems por
solicitud y 399 por carro; la referencia del cliente (CustomerPartNumber) admite hasta 21
caracteres y no puede contener «*».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .models import LEVEL_EXCLUDED, BomItem, ItemQuote
from .utils import normalize_pn, parse_int, parse_number, strip_accents

MAX_CUSTOMER_PN = 21

_TOKEN_RE = re.compile(r"^([A-Za-z_]+)(\d+)$")


def compact_designators(text: str) -> str:
    """"C1, C2, C3, C4, C7" -> "C1-C4,C7" (rangos de 3 o más designadores seguidos)."""
    tokens = [t for t in re.split(r"[,;\s]+", text or "") if t]
    out: list[str] = []
    run: list[tuple[str, int, str]] = []

    def flush() -> None:
        if len(run) >= 3:
            out.append(f"{run[0][2]}-{run[-1][2]}")
        else:
            out.extend(r[2] for r in run)
        run.clear()

    for token in tokens:
        match = _TOKEN_RE.match(token)
        if not match:
            flush()
            out.append(token)
            continue
        prefix, number = match.group(1).upper(), int(match.group(2))
        if run and (run[-1][0] != prefix or run[-1][1] + 1 != number):
            flush()
        run.append((prefix, number, token))
    flush()
    return ",".join(out)


def customer_reference(designators: str, fallback: str = "") -> str:
    """Referencia para el carro: designadores compactos, solo ASCII, sin «*», hasta 21 caracteres."""
    text = compact_designators(designators) or fallback
    text = strip_accents(text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text.replace("*", "")).strip()
    if len(text) <= MAX_CUSTOMER_PN:
        return text
    cut = text[:MAX_CUSTOMER_PN - 1]
    comma = cut.rfind(",")
    if comma >= 2:
        cut = cut[:comma]
    return cut.rstrip(",- ") + "+"  # «+»: hay más designadores de los que caben


@dataclass
class CartLine:
    """Una parte a comprar: código Mouser y cantidad (más datos para mostrar)."""

    mouser_pn: str
    quantity: int
    customer_pn: str = ""
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    ext_price: Decimal | None = None  # estimación local de la cotización
    currency: str = ""
    designators: str = ""
    item_ids: list[int] = field(default_factory=list)

    def to_api(self) -> dict:
        data: dict = {"MouserPartNumber": self.mouser_pn, "Quantity": int(self.quantity)}
        if self.customer_pn:
            data["CustomerPartNumber"] = self.customer_pn
        return data


def merge_lines(lines: list[CartLine]) -> list[CartLine]:
    """Une las líneas con el mismo código Mouser (suma cantidades y referencias)."""
    merged: dict[str, CartLine] = {}
    for line in lines:
        key = normalize_pn(line.mouser_pn)
        if not key or line.quantity <= 0:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = CartLine(line.mouser_pn, line.quantity, line.customer_pn, line.mpn, line.manufacturer,
                                   line.description, line.ext_price, line.currency, line.designators,
                                   list(line.item_ids))
            continue
        current.quantity += line.quantity
        current.designators = ", ".join(d for d in (current.designators, line.designators) if d)
        current.customer_pn = customer_reference(current.designators, current.customer_pn)
        if current.ext_price is not None and line.ext_price is not None:
            current.ext_price += line.ext_price
        else:
            current.ext_price = None
        current.item_ids.extend(line.item_ids)
    return list(merged.values())


def cart_lines(items: list[BomItem], quotes: list[ItemQuote]) -> list[CartLine]:
    """Partes de la cotización que se pueden comprar en Mouser, una línea por código Mouser."""
    lines = []
    for item, q in zip(items, quotes):
        if not item.include or q.level == LEVEL_EXCLUDED or q.part is None or not q.buy_qty:
            continue
        if not q.part.orderable or not q.part.mouser_pn:
            continue
        lines.append(CartLine(
            mouser_pn=q.part.mouser_pn,
            quantity=int(q.buy_qty),
            customer_pn=customer_reference(item.designators, f"Linea {item.rows_label}"),
            mpn=q.part.mpn,
            manufacturer=q.part.manufacturer,
            description=q.part.description,
            ext_price=q.ext_price,
            currency=q.currency,
            designators=item.designators,
            item_ids=[item.id],
        ))
    return merge_lines(lines)


@dataclass
class CartItemResult:
    """Un ítem del carro según la respuesta de Mouser."""

    mouser_pn: str
    quantity: int
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    customer_pn: str = ""
    unit_price: Decimal | None = None
    ext_price: Decimal | None = None
    available: int | None = None  # MouserATS: disponible para despacho
    min_qty: int | None = None
    mult: int | None = None
    lifecycle: str = ""
    errors: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict, currency: str = "") -> "CartItemResult":
        from .mouser_api import error_messages  # evita la importación circular

        def text(name: str) -> str:
            return str(raw.get(name) or "").strip()

        info = raw.get("InfoMessages") or []
        return cls(
            mouser_pn=text("MouserPartNumber"),
            quantity=parse_int(raw.get("Quantity")) or 0,
            mpn=text("MfrPartNumber"),
            manufacturer=text("Manufacturer"),
            description=text("Description"),
            customer_pn=text("CartItemCustPartNumber"),
            unit_price=parse_number(raw.get("UnitPrice"), currency),
            ext_price=parse_number(raw.get("ExtendedPrice"), currency),
            available=parse_int(raw.get("MouserATS")),
            min_qty=parse_int(raw.get("SalesMinimumOrderQty")),
            mult=parse_int(raw.get("SalesMultipleQty")),
            lifecycle=text("LifeCycle"),
            errors=error_messages(raw.get("Errors")),
            info=[str(m).strip() for m in (info if isinstance(info, list) else [info]) if str(m).strip()],
        )


@dataclass
class CartResult:
    """Resultado de crear el carro: clave, ítems según Mouser y errores."""

    cart_key: str = ""
    currency: str = ""
    items: list[CartItemResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # errores generales (no de un ítem)
    requested: list[CartLine] = field(default_factory=list)
    pending: list[CartLine] = field(default_factory=list)  # no se alcanzaron a enviar

    @property
    def total(self) -> Decimal:
        return sum((i.ext_price for i in self.items if i.ext_price is not None), Decimal("0"))

    @property
    def items_with_errors(self) -> list[CartItemResult]:
        return [i for i in self.items if i.errors]

    @property
    def missing(self) -> list[CartLine]:
        """Líneas enviadas que no aparecen en la respuesta de Mouser."""
        present = {normalize_pn(i.mouser_pn) for i in self.items}
        pending = {normalize_pn(p.mouser_pn) for p in self.pending}
        return [line for line in self.requested
                if normalize_pn(line.mouser_pn) not in present and normalize_pn(line.mouser_pn) not in pending]

    @property
    def ok(self) -> bool:
        return bool(self.cart_key) and not self.errors and not self.items_with_errors and not self.pending \
            and not self.missing
