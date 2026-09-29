import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fake_mouser import FakeMouserServer  # noqa: E402

from mouser_engine.mouser_api import MouserClient, RateLimiter  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Nunca escribir en la configuración real del usuario durante las pruebas."""
    monkeypatch.setenv("MOUSER_ENGINE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    monkeypatch.delenv("MOUSER_CART_API_KEY", raising=False)


@pytest.fixture
def server():
    return FakeMouserServer()


@pytest.fixture
def client(server):
    return MouserClient("test-key", opener=server.opener, rate_limiter=RateLimiter(1000, 60),
                        sleep=lambda s: None)


@pytest.fixture
def example_bom():
    return ROOT / "ejemplos" / "bom_ejemplo.csv"
