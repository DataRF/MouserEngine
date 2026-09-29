"""Servidor simulado de la Mouser Search API para pruebas (sin red).

Reemplaza a urllib.request.urlopen: recibe el Request que arma MouserClient y responde con
partes del archivo fixtures/mouser_parts.json siguiendo el formato de la API real.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mouser_engine.utils import normalize_pn, parse_number

FIXTURE = Path(__file__).parent / "fixtures" / "mouser_parts.json"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeMouserServer:
    def __init__(self, parts: list[dict] | None = None, valid_key: str = "test-key",
                 valid_cart_key: str = "cart-key"):
        self.parts = parts if parts is not None else json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.valid_key = valid_key
        self.valid_cart_key = valid_cart_key
        self.calls: list[dict] = []
        self.fail_next: list[object] = []  # excepciones o (código HTTP, cuerpo) a devolver en orden
        self.carts: dict[str, dict[str, dict]] = {}  # CartKey -> ítems por código Mouser
        self.orders: list[dict] = []  # la aplicación nunca debe llegar aquí (Order API)
        self._lock = threading.Lock()

    def opener(self, request, timeout=None):
        url = urlparse(request.full_url)
        key = parse_qs(url.query).get("apiKey", [""])[0]
        body = json.loads(request.data.decode("utf-8"))
        with self._lock:
            self.calls.append({"path": url.path, "body": body})
            failure = self.fail_next.pop(0) if self.fail_next else None
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, tuple):
            code, payload = failure
            raise urllib.error.HTTPError(request.full_url, code, "error", {},
                                         io.BytesIO(json.dumps(payload).encode()))
        if "/order" in url.path:
            self.orders.append(body)
            raise urllib.error.HTTPError(request.full_url, 400, "not allowed", {}, io.BytesIO(b"{}"))
        if "/cart" in url.path:
            return self._cart(url.path, key, body)
        if key != self.valid_key:
            return self._json({"Errors": [{"Id": 0, "Code": "Invalid", "Message": "Invalid unique identifier.",
                                           "ResourceKey": "InvalidIdentifier", "PropertyName": "API Key"}],
                               "SearchResults": None})
        if url.path.endswith("/search/partnumber"):
            req = body["SearchByPartRequest"]
            exact = req.get("partSearchOptions") == "Exact"
            found = []
            for pn in req["mouserPartNumber"].split("|"):
                key_pn = normalize_pn(pn)
                for part in self.parts:
                    mpn = normalize_pn(part["ManufacturerPartNumber"])
                    mouser = normalize_pn(part["MouserPartNumber"])
                    hit = key_pn in (mpn, mouser) if exact else (key_pn and (key_pn in mpn or key_pn in mouser))
                    if hit and part not in found:
                        found.append(part)
            return self._json({"Errors": [], "SearchResults": {"NumberOfResult": len(found), "Parts": found}})
        if url.path.endswith("/search/keyword"):
            req = body["SearchByKeywordRequest"]
            words = req["keyword"].lower().split()
            found = [p for p in self.parts
                     if all(w in (p["Description"] + " " + p["ManufacturerPartNumber"] + " "
                                  + p["Manufacturer"]).lower() for w in words)]
            if req.get("searchOptions") == "InStock":
                found = [p for p in found if int(p.get("AvailabilityInStock") or 0) > 0]
            limit = req.get("records", 50)
            return self._json({"Errors": [], "SearchResults": {"NumberOfResult": len(found),
                                                               "Parts": found[:limit]}})
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"{}"))

    # --- Cart API (formato de la guía de Mouser) ------------------------------------------

    def _cart(self, path: str, key: str, body: dict) -> _Response:
        if key != self.valid_cart_key:
            return self._json({"Errors": [{"Id": 0, "Code": "Invalid", "Message": "Invalid unique identifier.",
                                           "ResourceKey": "InvalidIdentifier", "PropertyName": "API Key"}],
                               "CartKey": None, "CartItems": []})
        if not path.endswith("/cart/items/insert"):
            raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO(b"{}"))
        items = body.get("CartItems") or []
        if not items:
            return self._json({"Errors": [{"Code": "EmptyCart", "Message": "At least one cart item is required."}],
                               "CartKey": body.get("CartKey") or None, "CartItems": []})
        cart_key = body.get("CartKey") or f"00000000-0000-0000-0000-{len(self.carts) + 1:012d}"
        cart = self.carts.setdefault(cart_key, {})
        if len(items) > 100 or len(cart) + len(items) > 399:
            return self._json({"Errors": [{"Code": "MaxCartItems", "Message": "Too many cart items."}],
                               "CartKey": cart_key, "CartItems": list(cart.values())})
        for entry in items:
            pn = str(entry.get("MouserPartNumber") or "")
            quantity = int(entry.get("Quantity") or 0)
            reference = str(entry.get("CustomerPartNumber") or "")
            part = next((p for p in self.parts if normalize_pn(p["MouserPartNumber"]) == normalize_pn(pn)), None)
            current = cart.get(normalize_pn(pn))
            total_qty = quantity + (current["Quantity"] if current else 0)
            cart[normalize_pn(pn)] = self._cart_item(pn, total_qty, reference, part)
        return self._json({"Errors": [], "CartKey": cart_key, "CurrencyCode": "USD",
                           "CartItems": list(cart.values()), "TotalItemCount": len(cart)})

    @staticmethod
    def _cart_item(pn: str, quantity: int, reference: str, part: dict | None) -> dict:
        errors = []
        if len(reference) > 21 or "*" in reference:
            errors.append({"Code": "InvalidCustomerPartNumber", "Message": "Invalid customer part number.",
                           "PropertyName": "CustomerPartNumber"})
        if part is None:
            errors.append({"Code": "InvalidMouserPartNumber", "Message": "Invalid Mouser part number.",
                           "PropertyName": "MouserPartNumber"})
            return {"Errors": errors, "MouserATS": 0, "Quantity": quantity, "MouserPartNumber": pn,
                    "MfrPartNumber": "", "Description": "", "CartItemCustPartNumber": reference,
                    "UnitPrice": 0, "ExtendedPrice": 0, "InfoMessages": []}
        minimum = int(part.get("Min") or 1)
        multiple = int(part.get("Mult") or 1)
        if quantity < minimum or quantity % multiple:
            errors.append({"Code": "InvalidQuantity",
                           "Message": f"Quantity must be at least {minimum} and a multiple of {multiple}.",
                           "PropertyName": "Quantity"})
        unit = 0.0
        for price_break in part.get("PriceBreaks") or []:
            price = parse_number(price_break.get("Price"), "USD")
            if price is not None and int(price_break["Quantity"]) <= quantity:
                unit = float(price)
        stock = int(part.get("AvailabilityInStock") or 0)
        info = [] if stock >= quantity else [f"{quantity - stock} will be backordered."]
        return {"Errors": errors, "MouserATS": stock, "Quantity": quantity, "PartsPerReel": 0,
                "ScheduledReleases": [], "InfoMessages": info, "MouserPartNumber": part["MouserPartNumber"],
                "MfrPartNumber": part["ManufacturerPartNumber"], "Description": part["Description"],
                "CartItemCustPartNumber": reference, "UnitPrice": unit, "ExtendedPrice": round(unit * quantity, 2),
                "LifeCycle": part.get("LifecycleStatus") or "", "Manufacturer": part["Manufacturer"],
                "SalesMultipleQty": multiple, "SalesMinimumOrderQty": minimum}

    @staticmethod
    def _json(payload: dict) -> _Response:
        return _Response(json.dumps(payload).encode("utf-8"))
