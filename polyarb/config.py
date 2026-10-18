"""Central configuration, risk caps, and circuit breakers for polyarb."""
from __future__ import annotations
import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RiskLimits:
    max_capital_per_relation: float = 500.0      # max USD deployed in one relation
    max_capital_per_event: float = 1000.0        # max USD across relations sharing an event
    max_capital_per_venue: float = 2500.0        # max USD committed to a single venue
    max_total_exposure: float = 5000.0           # total portfolio locked capital cap
    daily_loss_limit: float = 100.0              # halt if realized/unwind loss exceeds this
    min_edge: float = 0.005                      # $0.005 per share minimum edge
    max_anomaly_edge: float = 0.35               # edges > 35c are almost always data bugs or rule mismatches
    min_annualized_return: float = 0.15          # 15% APY hurdle
    min_shares: float = 5.0                      # minimum basket size
    min_depth_thinnest_leg: float = 5.0          # require depth on the thinnest leg
    max_slippage_bps: float = 50.0               # 0.5% max allowable execution slippage
    max_clock_skew_seconds: float = 3.0          # cross-venue quotes must be within 3s of each other
    consecutive_obs_required: int = 2            # phantom-arb filter: must see edge in >= 2 consecutive ticks
    approval_expiry_days: int = 30               # human approvals expire after N days
    simulate_execution_delay_s: float = 0.25     # execution delay between leg 1 and leg 2 in replay/simulation
    model_void_outcomes: bool = False            # include void/refund worlds in worst-case payoff (stress test mode)


@dataclass
class FeeConfig:
    polymarket_taker_bps: float = 0.0            # Polymarket default taker fee bps
    kalshi_taker_fee_enabled: bool = True        # Kalshi SEC/regulatory fee calculation


@dataclass
class Config:
    risk: RiskLimits = field(default_factory=RiskLimits)
    fees: FeeConfig = field(default_factory=FeeConfig)
    kill_switch_file: str = "KILL_SWITCH"
    data_dir: str = "data"
    polymarket_default_fee_bps: float = 0.0      # Polymarket default taker fee bps
    non_trading_cost_usd: float = 0.05           # estimated gas / bridging amortization per basket trade

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        cfg = cls()
        p = Path(path) if path else Path(cfg.data_dir) / "config.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "risk" in data and isinstance(data["risk"], dict):
                    for k, v in data["risk"].items():
                        if hasattr(cfg.risk, k):
                            setattr(cfg.risk, k, v)
                if "fees" in data and isinstance(data["fees"], dict):
                    for k, v in data["fees"].items():
                        if hasattr(cfg.fees, k):
                            setattr(cfg.fees, k, v)
                for k, v in data.items():
                    if k not in ("risk", "fees") and hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception as e:
                print(f"[config] Warning loading {p}: {e}")
        return cfg


DEFAULT_CONFIG = Config()


def is_hard_kill_active(data_dir: str | Path = "data") -> bool:
    """Check if emergency hard kill switch is active (cancels resting orders and halts all trading)."""
    if os.environ.get("POLYARB_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes", "stop"):
        return True
    d = Path(data_dir)
    return (d / "KILL_SWITCH").exists() or Path("KILL_SWITCH").exists()


def is_soft_kill_active(data_dir: str | Path = "data") -> bool:
    """Check if soft kill mode is active (stops opening new trades, lets existing ride to settlement)."""
    if is_hard_kill_active(data_dir):
        return True
    if os.environ.get("POLYARB_SOFT_KILL", "").strip().lower() in ("1", "true", "yes"):
        return True
    d = Path(data_dir)
    return (d / "SOFT_KILL").exists() or Path("SOFT_KILL").exists()


def is_kill_switch_active(data_dir: str | Path = "data") -> bool:
    """Default kill switch check (True if either soft or hard kill is triggered)."""
    return is_soft_kill_active(data_dir)


def trigger_kill_switch(reason: str, data_dir: str | Path = "data", mode: str = "hard"):
    """Write kill switch file to immediately halt execution. mode='hard' | 'soft'."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    filename = "SOFT_KILL" if mode.lower() == "soft" else "KILL_SWITCH"
    p = d / filename
    p.write_text(f"KILLED ({mode.upper()}): {reason}\n", encoding="utf-8")
    print(f"[CIRCUIT BREAKER TRIGGERED] {mode.upper()} KILL: {reason} -> written to {p}")


def clear_kill_switch(data_dir: str | Path = "data", mode: str = "all"):
    """Clears kill switch files and resets environment variables."""
    d = Path(data_dir)
    if mode in ("all", "hard"):
        p = d / "KILL_SWITCH"
        if p.exists(): p.unlink()
        if Path("KILL_SWITCH").exists(): Path("KILL_SWITCH").unlink()
        if "POLYARB_KILL_SWITCH" in os.environ: del os.environ["POLYARB_KILL_SWITCH"]
    if mode in ("all", "soft"):
        p = d / "SOFT_KILL"
        if p.exists(): p.unlink()
        if Path("SOFT_KILL").exists(): Path("SOFT_KILL").unlink()
        if "POLYARB_SOFT_KILL" in os.environ: del os.environ["POLYARB_SOFT_KILL"]


def get_kill_status(data_dir: str | Path = "data") -> dict[str, Any]:
    hard = is_hard_kill_active(data_dir)
    soft = is_soft_kill_active(data_dir)
    reason = ""
    d = Path(data_dir)
    if (d / "KILL_SWITCH").exists():
        reason = (d / "KILL_SWITCH").read_text(encoding="utf-8").strip()
    elif (d / "SOFT_KILL").exists():
        reason = (d / "SOFT_KILL").read_text(encoding="utf-8").strip()
    return {
        "active": hard or soft,
        "hard_kill": hard,
        "soft_kill": soft,
        "reason": reason,
    }
