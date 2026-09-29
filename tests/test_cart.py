import io
import urllib.error
from decimal import Decimal

import pytest

from mouser_engine.bom import build_items, load_table
from mouser_engine.cart import CartLine, cart_lines, compact_designators, customer_reference, merge_lines
from mouser_engine.lookup import lookup_all
from mouser_engine.models import QuoteParams
from mouser_engine.mouser_api import (
    MAX_CART_ITEMS,
    MouserAuthError,
    MouserClient,
    MouserConnectionError,
    MouserError,
    RateLimiter,
)
from mouser_engine.passives import Defaults
from mouser_engine.quote import apply_lookup, queries_for, quote_all, specs_for


@pytest.fixture
def cart_client(server):
    return MouserClient("cart-key", opener=server.opener, rate_limiter=RateLimiter(1000, 60), sleep=lambda s: None)


@pytest.fixture
def quoted(client, example_bom):
    items, _ = build_items(load_table(example_bom))
    params = QuoteParams(boards=10, passive_spares_pct=10)
    specs, required = specs_for(items, params)
    bundle = lookup_all(client, queries_for(items), specs, Defaults(), required)
    apply_lookup(items, bundle.parts, spec_results=bundle.specs)
    return items, quote_all(items, params)


def test_compact_designators():
    assert compact_designators("C1, C2, C3, C4, C7") == "C1-C4,C7"
    assert compact_designators("R1 R2") == "R1,R2"
    assert compact_designators("U1;U2;U3;J1") == "U1-U3,J1"
    assert compact_designators("R1-R10, R12") == "R1-R10,R12"
    assert compact_designators("") == ""


def test_customer_reference_respects_mouser_rules():
    assert customer_reference("C1, C2, C3, C4, C5, C6, C7, C8") == "C1-C8"
    long = customer_reference("R1, R3, R5, R7, R9, R11, R13, R15, R17")
    assert len(long) <= 21 and long.endswith("+") and "*" not in long
    assert long.startswith("R1,R3,R5")
    assert customer_reference("", "Linea 7") == "Linea 7"
    assert customer_reference("C*1, Ñ2") == "C1,N2"
    assert len(customer_reference("X" * 40)) <= 21


def test_cart_lines_from_quote(quoted):
    items, quotes = quoted
    lines = cart_lines(items, quotes)
    pns = [line.mouser_pn for line in lines]
    assert "81-GRM188R71C104KA1D" in pns
    assert "700-MAX232CPE" not in pns  # sin precio: no se puede comprar
    assert len(pns) == len(set(pns))
    assert all(len(line.customer_pn) <= 21 and "*" not in line.customer_pn for line in lines)
    cap = next(line for line in lines if line.mouser_pn == "81-GRM188R71C104KA1D")
    assert cap.quantity == 88 and cap.customer_pn.startswith("C1")


def test_merge_lines_sums_same_mouser_part():
    lines = [CartLine("71-CRCW06034K70FKEA", 20, "R1-R20", designators="R1-R20", ext_price=Decimal("1.00"),
                      item_ids=[1]),
             CartLine("71-CRCW06034K70FKEA", 10, "R30", designators="R30", ext_price=Decimal("0.50"), item_ids=[2]),
             CartLine("81-GRM188R71C104KA1D", 5, "C1")]
    merged = merge_lines(lines)
    assert [(m.mouser_pn, m.quantity) for m in merged] == [("71-CRCW06034K70FKEA", 30), ("81-GRM188R71C104KA1D", 5)]
    assert merged[0].customer_pn == "R1-R20,R30" and merged[0].ext_price == Decimal("1.50")
    assert merged[0].item_ids == [1, 2]
    assert lines[0].quantity == 20  # no modifica las líneas originales


def test_cart_insert_creates_cart(server, cart_client, quoted):
    items, quotes = quoted
    lines = cart_lines(items, quotes)
    result = cart_client.cart_insert(lines)
    call = server.calls[-1]
    assert call["path"] == "/api/v1.0/cart/items/insert"
    assert call["body"]["CartKey"] == ""
    assert call["body"]["CartItems"][0] == {"MouserPartNumber": lines[0].mouser_pn, "Quantity": lines[0].quantity,
                                            "CustomerPartNumber": lines[0].customer_pn}
    assert result.cart_key and result.currency == "USD"
    assert {i.mouser_pn for i in result.items} == {line.mouser_pn for line in lines}
    assert result.total > 0 and not result.errors and not result.missing
    assert not server.orders  # nunca se usa la Order API


def test_cart_insert_reports_item_errors(cart_client):
    lines = [CartLine("81-GRM188R71C104KA1D", 10, "C1"), CartLine("999-NOEXISTE", 1, "U9"),
             CartLine("595-LM358DR", 5, "U2")]
    result = cart_client.cart_insert(lines)
    assert result.cart_key
    bad = {i.mouser_pn: i for i in result.items_with_errors}
    assert "999-NOEXISTE" in bad and "Invalid Mouser part number" in bad["999-NOEXISTE"].errors[0]
    lm358 = next(i for i in result.items if i.mouser_pn == "595-LM358DR")
    assert lm358.info and "backordered" in lm358.info[0]  # sin stock: informativo, no es error
    assert not result.ok


def test_cart_insert_batches_of_100_share_cart_key(server, cart_client):
    part = server.parts[0]
    extra = [{**part, "MouserPartNumber": f"000-FAKE{i:03d}"} for i in range(149)]
    server.parts.extend(extra)
    lines = [CartLine(part["MouserPartNumber"], 10)] + [CartLine(p["MouserPartNumber"], 10) for p in extra]
    progress = []
    result = cart_client.cart_insert(lines, progress=lambda d, t, m: progress.append((d, t)))
    calls = [c for c in server.calls if c["path"].endswith("/cart/items/insert")]
    assert [len(c["body"]["CartItems"]) for c in calls] == [100, 50]
    assert calls[0]["body"]["CartKey"] == "" and calls[1]["body"]["CartKey"] == result.cart_key
    assert len(result.items) == 150 and len(server.carts) == 1
    assert progress[-1] == (2, 2)


def test_cart_insert_stops_without_cart_key(cart_client):
    lines = [CartLine(f"000-FAKE{i:03d}", 1) for i in range(150)]
    calls = []

    def opener(request, timeout=None):  # Mouser responde con un error y sin CartKey
        calls.append(request)
        return io.BytesIO(b'{"Errors":[{"Code":"Invalid","Message":"Request data is missing or malformed"}],'
                          b'"CartKey":null}')

    cart_client._opener = opener
    result = cart_client.cart_insert(lines)
    assert not result.cart_key and "malformed" in result.errors[0]
    assert len(calls) == 1 and len(result.pending) == 50  # el segundo lote habría creado otro carro


def test_cart_insert_limits():
    client = MouserClient("cart-key", opener=lambda *a, **k: None)
    with pytest.raises(MouserError, match="399"):
        client.cart_insert([CartLine(f"000-P{i}", 1) for i in range(MAX_CART_ITEMS + 1)])
    with pytest.raises(MouserError, match="No hay partes"):
        client.cart_insert([CartLine("000-P1", 0)])


def test_cart_insert_invalid_key(server):
    client = MouserClient("test-key", opener=server.opener, rate_limiter=RateLimiter(1000, 60))
    with pytest.raises(MouserAuthError, match="Cart API"):
        client.cart_insert([CartLine("595-LM358DR", 5)])


def test_cart_insert_is_not_retried_after_connection_loss(server, cart_client):
    server.fail_next = [urllib.error.URLError("timed out")]
    with pytest.raises(MouserConnectionError):
        cart_client.cart_insert([CartLine("595-LM358DR", 5)])
    assert len(server.calls) == 1  # un reintento podría duplicar el carro


def test_cart_insert_later_batch_failure_keeps_created_cart(server, cart_client):
    part = server.parts[0]
    extra = [{**part, "MouserPartNumber": f"000-FAKE{i:03d}"} for i in range(120)]
    server.parts.extend(extra)
    lines = [CartLine(p["MouserPartNumber"], 10) for p in extra]
    original = server.opener
    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        if len(calls) == 2:
            raise urllib.error.URLError("connection reset")
        return original(request, timeout)

    cart_client._opener = opener
    result = cart_client.cart_insert(lines)
    assert result.cart_key and len(result.items) == 100
    assert len(result.pending) == 20 and result.errors and not result.ok
