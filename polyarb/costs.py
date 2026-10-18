"""Dynamic Gas and Non-Trading Cost Model (Phase 4.4).
Replaces the flat $0.05 non-trading cost with a dynamic model:
- Live Polygon gas price estimation (gwei)
- Transaction execution gas (approval + order execution)
- MATIC/USD valuation
- Amortized bridging and deposit/withdrawal overhead
- Seamless deduction from net edge in candidate scoring
"""
from __future__ import annotations
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from .logger import get_logger

logger = get_logger("costs")

BASELINE_POLYGON_GAS_GWEI = 45.0
BASELINE_MATIC_USD = 0.50
DEFAULT_AMORTIZED_BRIDGE_USD = 0.02  # $0.50 deposit amortized over 25 trades
GAS_UNITS_PER_POLY_LEG = 110_000     # CLOB fill + token approval


@dataclass
class CostModelRecord:
    polygon_gas_gwei: float = BASELINE_POLYGON_GAS_GWEI
    matic_usd: float = BASELINE_MATIC_USD
    amortized_bridge_usd: float = DEFAULT_AMORTIZED_BRIDGE_USD
    last_updated: float = 0.0
    source: str = "default_baseline"


class CostModelService:
    """Dynamic fee service tracking live Polygon gas prices and non-trading overhead."""
    _instance: CostModelService | None = None
    _lock = threading.Lock()

    def __init__(self, ttl_sec: float = 300.0):
        self.ttl_sec = ttl_sec
        self.record = CostModelRecord()

    @classmethod
    def get_instance(cls) -> CostModelService:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = CostModelService()
        return cls._instance

    def update_gas_price(self, force_refresh: bool = False) -> float:
        """Fetches live Polygon gas price in gwei from Polygon Gas Station / public RPC."""
        now = time.time()
        if not force_refresh and (now - self.record.last_updated) < self.ttl_sec and self.record.last_updated > 0:
            return self.record.polygon_gas_gwei

        # Attempt to fetch live Polygon gas station
        try:
            req = urllib.request.Request(
                "https://gasstation.polygon.technology/v2",
                headers={"User-Agent": "PolyArb/1.0"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    standard = data.get("standard", {}).get("maxFee")
                    if standard and float(standard) > 0:
                        self.record.polygon_gas_gwei = round(float(standard), 2)
                        self.record.last_updated = now
                        self.record.source = "polygon_gasstation_v2"
                        logger.info(f"Updated live Polygon gas: {self.record.polygon_gas_gwei:.1f} gwei")
                        return self.record.polygon_gas_gwei
        except Exception as e:
            logger.debug(f"Polygon gas station fetch warning: {e}")

        if self.record.last_updated == 0:
            self.record.polygon_gas_gwei = BASELINE_POLYGON_GAS_GWEI
            self.record.last_updated = now
            self.record.source = "baseline_fallback"

        return self.record.polygon_gas_gwei

    def calculate_non_trading_cost(self, venue: str = "polymarket", leg_count: int = 2) -> float:
        """Calculates expected non-trading execution cost in USD for a trade structure."""
        gas_gwei = self.update_gas_price()
        v = venue.lower().strip()

        # Kalshi is purely cash/REST without on-chain gas
        if "kalshi" in v and "poly" not in v and "manifold" not in v:
            # Flat amortized banking/ACH cost
            return 0.01

        # Manifold is purely web/REST with no gas
        if "manifold" in v and "poly" not in v:
            return 0.005

        # Polymarket / Cross-venue involving Polygon:
        # Gas in MATIC = (gas_units * gas_gwei * 1e9) / 1e18 = (gas_units * gas_gwei) / 1e9
        total_gas_units = GAS_UNITS_PER_POLY_LEG * max(1, leg_count)
        gas_matic = (total_gas_units * gas_gwei) / 1e9
        gas_usd = gas_matic * self.record.matic_usd

        total_cost = round(gas_usd + self.record.amortized_bridge_usd, 4)
        return max(0.01, total_cost)

    def get_status(self) -> dict[str, Any]:
        return {
            "polygon_gas_gwei": self.record.polygon_gas_gwei,
            "matic_usd": self.record.matic_usd,
            "amortized_bridge_usd": self.record.amortized_bridge_usd,
            "source": self.record.source,
            "sample_cost_poly_usd": self.calculate_non_trading_cost("polymarket", 2),
            "sample_cost_kalshi_usd": self.calculate_non_trading_cost("kalshi", 2),
        }


def get_non_trading_cost(venue: str = "polymarket", leg_count: int = 2) -> float:
    return CostModelService.get_instance().calculate_non_trading_cost(venue=venue, leg_count=leg_count)
