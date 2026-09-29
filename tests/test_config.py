import json
from datetime import date

from mouser_engine.config import Settings, config_dir


def test_config_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUSER_ENGINE_CONFIG_DIR", str(tmp_path / "x"))
    assert config_dir() == tmp_path / "x"


def test_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    rates = {"usd": "942.25", "usd_date": "2026-09-29", "customs": "933.9", "customs_date": "2026-08-28"}
    s = Settings(api_key="abc", boards=25, landed_cost=True, optimize_breaks=True, import_rates=rates)
    s.save(path)
    loaded = Settings.load(path)
    assert (loaded.api_key, loaded.boards, loaded.landed_cost, loaded.optimize_breaks) == ("abc", 25, True, True)
    assert loaded.import_rates == rates and loaded.import_rules == {}


def test_old_manual_costs_are_ignored(tmp_path):
    # versiones anteriores guardaban flete, arancel, IVA y tipo de cambio ingresados a mano
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"freight": 40, "duty_pct": 6, "vat_pct": 19, "fx_rate": 950, "import_rates": [1]}),
                    encoding="utf-8")
    loaded = Settings.load(path)
    assert not hasattr(loaded, "freight") and not loaded.landed_cost and loaded.import_rates == {}


def test_load_ignores_unknown_and_bad_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_key": "k", "boards": "not a number", "nuevo": 1}), encoding="utf-8")
    loaded = Settings.load(path)
    assert loaded.api_key == "k" and loaded.boards == 1


def test_load_missing_or_corrupt(tmp_path):
    assert Settings.load(tmp_path / "nada.json").api_key == ""
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert Settings.load(bad).boards == 1


def test_env_key_has_priority(monkeypatch):
    s = Settings(api_key="archivo")
    assert s.effective_api_key == "archivo" and not s.api_key_from_env
    monkeypatch.setenv("MOUSER_API_KEY", " entorno ")
    assert s.effective_api_key == "entorno" and s.api_key_from_env


def test_usage_counter_resets_daily():
    s = Settings(usage_date="2000-01-01", usage_count=500)
    assert s.calls_today == 0
    s.register_call()
    assert s.usage_date == date.today().isoformat() and s.calls_today == 1
    assert s.calls_left == 999
