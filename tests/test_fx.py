"""Unit tests for Dynamic Real-Time FX Rate Service (Phase 3.6)."""
from __future__ import annotations
import time
from polyarb.fx import FxService, FxRateRecord, BASELINE_INR_PER_USD, get_current_fx_rate


def test_fx_default_baseline():
    svc = FxService(ttl_sec=3600.0)
    # If not updated, rate defaults to BASELINE_INR_PER_USD
    rate = svc.get_rate()
    assert rate > 10.0
    status = svc.get_status()
    assert status["base"] == "USD"
    assert status["quote"] == "INR"
    assert "rate" in status
    assert "source" in status


def test_fx_conversions():
    svc = FxService(ttl_sec=3600.0)
    svc.record.rate = 83.50
    svc.record.last_updated = time.time()

    usd = 100.0
    inr = svc.usd_to_inr(usd)
    assert inr == 8350.0

    inr_back = svc.inr_to_usd(8350.0)
    assert abs(inr_back - 100.0) < 0.01


def test_fx_cache_ttl():
    svc = FxService(ttl_sec=10.0)
    svc.record.rate = 84.00
    svc.record.last_updated = time.time()
    svc.record.source = "test_cache"

    # Should return cached without hitting network
    rate = svc.get_rate(force_refresh=False)
    assert rate == 84.00
    assert svc.record.source == "test_cache"


def test_fx_fallback_on_network_error():
    svc = FxService(ttl_sec=1.0)
    # Monkeypatch _fetch_live_rate to return None
    svc._fetch_live_rate = lambda: (None, "error_fallback")
    svc.record.last_updated = 0.0

    rate = svc.get_rate()
    assert rate == BASELINE_INR_PER_USD
    assert svc.record.source == "baseline_fallback"


def test_global_helper():
    rate = get_current_fx_rate()
    assert rate > 10.0


if __name__ == "__main__":
    test_fx_default_baseline()
    print("ok test_fx_default_baseline")
    test_fx_conversions()
    print("ok test_fx_conversions")
    test_fx_cache_ttl()
    print("ok test_fx_cache_ttl")
    test_fx_fallback_on_network_error()
    print("ok test_fx_fallback_on_network_error")
    test_global_helper()
    print("ok test_global_helper")
