"""Dynamic Real-Time FX Rate Service (Phase 3.6).
Fetches live INR/USD exchange rates, caches with 1-hour TTL,
and gracefully falls back to baseline (83.50) if offline or rate-limited.
"""
from __future__ import annotations
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .logger import get_logger

logger = get_logger("fx")

BASELINE_INR_PER_USD = 83.50
CACHE_TTL_SEC = 3600.0  # 1 hour


@dataclass
class FxRateRecord:
    base: str = "USD"
    quote: str = "INR"
    rate: float = BASELINE_INR_PER_USD
    last_updated: float = 0.0
    source: str = "default_baseline"


class FxService:
    """Thread-safe dynamic FX rate provider with automatic caching and graceful fallback."""
    _instance: FxService | None = None
    _lock = threading.Lock()

    def __init__(self, ttl_sec: float = CACHE_TTL_SEC):
        self.ttl_sec = ttl_sec
        self.record = FxRateRecord()

    @classmethod
    def get_instance(cls) -> FxService:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = FxService()
        return cls._instance

    def get_rate(self, force_refresh: bool = False) -> float:
        """Returns the current INR per USD exchange rate."""
        now = time.time()
        if not force_refresh and (now - self.record.last_updated) < self.ttl_sec and self.record.last_updated > 0:
            return self.record.rate

        # Attempt to fetch live rate
        rate, source = self._fetch_live_rate()
        if rate and rate > 10.0:  # Sanity check: 1 USD > 10 INR
            self.record.rate = round(rate, 4)
            self.record.last_updated = now
            self.record.source = source
            logger.info(f"Updated live FX rate: 1 USD = {self.record.rate:.2f} INR (via {source})")
        else:
            if self.record.last_updated == 0:
                self.record.rate = BASELINE_INR_PER_USD
                self.record.last_updated = now
                self.record.source = "baseline_fallback"

        return self.record.rate

    def _fetch_live_rate(self) -> tuple[float | None, str]:
        """Attempts to fetch from public free FX APIs with timeout."""
        apis = [
            ("https://open.er-api.com/v6/latest/USD", lambda d: d.get("rates", {}).get("INR"), "open.er-api.com"),
            ("https://api.frankfurter.app/latest?from=USD&to=INR", lambda d: d.get("rates", {}).get("INR"), "frankfurter.app"),
        ]

        for url, extract_fn, src_name in apis:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PolyArb/1.0"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        rate = extract_fn(data)
                        if rate:
                            return float(rate), src_name
            except Exception as e:
                logger.debug(f"FX fetch from {src_name} failed: {e}")

        return None, "fallback"

    def usd_to_inr(self, amount_usd: float) -> float:
        return round(amount_usd * self.get_rate(), 2)

    def inr_to_usd(self, amount_inr: float) -> float:
        rate = self.get_rate()
        return round(amount_inr / max(rate, 1e-6), 4)

    def get_status(self) -> dict[str, Any]:
        return {
            "base": self.record.base,
            "quote": self.record.quote,
            "rate": self.record.rate,
            "last_updated_iso": datetime.fromtimestamp(self.record.last_updated, tz=timezone.utc).isoformat() if self.record.last_updated > 0 else "Never",
            "source": self.record.source,
        }


def get_current_fx_rate() -> float:
    return FxService.get_instance().get_rate()
