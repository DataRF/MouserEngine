import threading
import urllib.error
from decimal import Decimal

import pytest

from fake_mouser import FakeMouserServer
from mouser_engine.models import Part
from mouser_engine.mouser_api import (
    MouserAuthError,
    MouserCancelled,
    MouserClient,
    MouserConnectionError,
    MouserError,
    MouserRateLimitError,
    RateLimiter,
)


def make_client(server, key="test-key", **kwargs):
    kwargs.setdefault("rate_limiter", RateLimiter(1000, 60))
    kwargs.setdefault("sleep", lambda s: None)
    return MouserClient(key, opener=server.opener, **kwargs)


def test_request_format(server, client):
    parts = client.search_part_numbers(["LM358DR", "STM32F103C8T6"])
    call = server.calls[0]
    assert call["path"] == "/api/v1/search/partnumber"
    assert call["body"] == {"SearchByPartRequest": {"mouserPartNumber": "LM358DR|STM32F103C8T6",
                                                    "partSearchOptions": "Exact"}}
    assert {p.mpn for p in parts} == {"LM358DR", "STM32F103C8T6"}


def test_part_parsing(client):
    part = client.search_part_numbers(["GRM188R71C104KA01D"])[0]
    assert part.mouser_pn == "81-GRM188R71C104KA1D"
    assert part.stock == 1_250_000
    assert part.min_qty == 1 and part.mult == 1
    assert [pb.quantity for pb in part.price_breaks] == [1, 10, 100, 1000, 4000]
    assert part.price_breaks[1].price == Decimal("0.021")
    assert part.currency == "USD"
    assert part.packaging == "Reel, Cut Tape, MouseReel"
    assert part.lead_time_days == 112
    assert part.lifecycle_level == "ok"


def test_part_parsing_edge_cases():
    part = Part.from_api({
        "MouserPartNumber": "595-LM358DR", "ManufacturerPartNumber": "LM358DR", "Manufacturer": "TI",
        "Availability": "On Order", "AvailabilityInStock": None,
        "AvailabilityOnOrder": [{"Quantity": 2500, "Date": "2026-11-10T00:00:00"}],
        "Min": None, "Mult": "", "PriceBreaks": [{"Quantity": 1, "Price": "N/A", "Currency": "USD"}],
        "LifecycleStatus": "Obsolete", "SuggestedReplacement": "LM358BIDR",
        "SurchargeMessages": [{"code": "T", "message": "Tariff may apply"}],
    })
    assert part.stock == 0
    assert part.on_order[0].quantity == 2500 and part.on_order[0].date == "2026-11-10"
    assert part.min_qty == 1 and part.mult == 1
    assert part.price_breaks == [] and not part.orderable
    assert part.lifecycle_level == "error" and part.lifecycle_label == "Obsoleto"
    assert "Tariff may apply" in part.info_messages
    assert Part.from_api({"Availability": "1,234 In Stock"}).stock == 1234


def test_invalid_key_raises_auth_error(server):
    client = make_client(server, key="otra")
    with pytest.raises(MouserAuthError, match="Search API"):
        client.search_part_numbers(["LM358DR"])


def test_missing_key():
    with pytest.raises(MouserAuthError):
        MouserClient("").search_part_numbers(["LM358DR"])


def test_http_401_is_auth_error(server, client):
    server.fail_next = [(401, {"Message": "Authorization has been denied for this request."})]
    with pytest.raises(MouserAuthError, match="denied"):
        client.search_part_numbers(["LM358DR"])


def test_server_error_is_retried(server, client):
    server.fail_next = [(500, {"Errors": [{"Code": "InternalError", "Message": "boom"}]})]
    assert client.search_part_numbers(["LM358DR"])
    assert len(server.calls) == 2


def test_rate_limit_waits_and_retries(server):
    waits = []
    client = make_client(server, sleep=waits.append)
    server.fail_next = [(429, {"Errors": [{"Code": "TooManyRequests", "Message": "Too many requests"}]})]
    assert client.search_part_numbers(["LM358DR"])
    assert sum(waits) >= 59  # esperó ~60 s antes de reintentar


def test_daily_limit_is_not_retried(server, client):
    server.fail_next = [(429, {"Errors": [{"Message": "Maximum calls per day exceeded"}]})]
    with pytest.raises(MouserRateLimitError, match="diario"):
        client.search_part_numbers(["LM358DR"])
    assert len(server.calls) == 1


def test_proxy_block_is_reported_without_retries(server, client):
    server.fail_next = [urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))]
    with pytest.raises(MouserConnectionError, match="proxy"):
        client.search_part_numbers(["LM358DR"])
    assert len(server.calls) == 1


def test_connection_errors_are_retried_then_reported(server, client):
    server.fail_next = [urllib.error.URLError("timed out")] * 4
    with pytest.raises(MouserConnectionError):
        client.search_part_numbers(["LM358DR"])
    assert len(server.calls) == 4


def test_request_error_in_body(server, client):
    server.fail_next = []
    original = server.opener

    def opener(request, timeout=None):
        response = original(request, timeout)
        return type(response)(b'{"Errors":[{"Code":"Invalid","Message":"Request data is missing or malformed"}]}')

    client._opener = opener
    with pytest.raises(MouserError, match="malformed"):
        client.search_part_numbers(["LM358DR"])


def test_lookup_batches_ten_per_call(server, client):
    queries = [f"FAKE-{i}" for i in range(23)] + ["LM358DR"]
    progress = []
    results = client.lookup(queries, fuzzy=False, progress=lambda d, t, m: progress.append((d, t)))
    batch_calls = [c for c in server.calls if c["body"]["SearchByPartRequest"]["partSearchOptions"] == "Exact"]
    assert len(batch_calls) == 3
    assert results["LM358DR"].exact[0].mouser_pn == "595-LM358DR"
    assert not results["FAKE-0"].exact
    assert progress[-1] == (24, 24)


def test_lookup_dedupes_queries(server, client):
    results = client.lookup(["LM358DR", "lm358dr", "LM358-DR"], fuzzy=False)
    assert len(results) == 1
    assert len(server.calls) == 1


def test_lookup_multiple_manufacturers(client):
    result = client.lookup(["1N4148"], fuzzy=False)["1N4148"]
    assert {p.manufacturer for p in result.exact} == {"onsemi", "Vishay Semiconductors"}


def test_lookup_near_match_from_single_query():
    server = FakeMouserServer()
    # Mouser devuelve el MPN escrito distinto de lo buscado (p. ej. sin sufijo de empaque)
    server.parts.append({**server.parts[3], "ManufacturerPartNumber": "LM358DRG4",
                         "MouserPartNumber": "595-LM358DRG4"})
    original = server.opener

    def opener(request, timeout=None):
        import json
        body = json.loads(request.data.decode())
        if body["SearchByPartRequest"]["mouserPartNumber"] == "LM358-DR-G4X":
            request.data = json.dumps({"SearchByPartRequest": {"mouserPartNumber": "LM358DRG4",
                                                               "partSearchOptions": "Exact"}}).encode()
        return original(request, timeout)

    client = MouserClient("test-key", opener=opener, rate_limiter=RateLimiter(1000, 60), sleep=lambda s: None)
    result = client.lookup(["LM358-DR-G4X"], fuzzy=False)["LM358-DR-G4X"]
    assert not result.exact
    assert [p.mpn for p in result.near] == ["LM358DRG4"]


def test_lookup_suggestions_for_not_found(client, server):
    result = client.lookup(["GRM188R71C104"], fuzzy=True)["GRM188R71C104"]
    assert not result.exact
    assert [p.mpn for p in result.suggestions] == ["GRM188R71C104KA01D"]
    assert server.calls[-1]["body"]["SearchByPartRequest"]["partSearchOptions"] == "None"


def test_lookup_can_be_cancelled(client):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MouserCancelled):
        client.lookup(["LM358DR"], cancel=cancel)


def test_keyword_search(server, client):
    total, parts = client.search_keyword("resistors 0603", in_stock_only=True)
    assert total == 2 and len(parts) == 2
    body = server.calls[0]["body"]["SearchByKeywordRequest"]
    assert body["searchOptions"] == "InStock" and body["records"] == 50


def test_test_connection_reports_currency(client):
    assert "USD" in client.test_connection()


def test_on_request_counts_calls(server):
    calls = []
    client = make_client(server, on_request=lambda: calls.append(1))
    client.lookup(["LM358DR", "STM32F103C8T6"], fuzzy=False)
    assert len(calls) == 1


def test_rate_limiter_sliding_window():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(max_calls=3, period=60, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert slept == []
    limiter.wait()  # la cuarta espera a que se libere la ventana
    assert sum(slept) >= 60
