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

from mouser_engine.utils import normalize_pn

FIXTURE = Path(__file__).parent / "fixtures" / "mouser_parts.json"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeMouserServer:
    def __init__(self, parts: list[dict] | None = None, valid_key: str = "test-key"):
        self.parts = parts if parts is not None else json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.valid_key = valid_key
        self.calls: list[dict] = []
        self.fail_next: list[object] = []  # excepciones o (código HTTP, cuerpo) a devolver en orden
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

    @staticmethod
    def _json(payload: dict) -> _Response:
        return _Response(json.dumps(payload).encode("utf-8"))
