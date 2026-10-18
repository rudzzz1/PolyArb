"""Unit tests for controls, circuit breakers, limits, and kill switch."""
from __future__ import annotations
import os, sqlite3
from pathlib import Path
from polyarb.config import Config, is_kill_switch_active, trigger_kill_switch
from polyarb.core import Level
from polyarb import scanner


def _db():
    db = sqlite3.connect(":memory:")
    db.executescript(scanner.SCHEMA)
    return db


def test_kill_switch_env_and_file(tmp_path: Path):
    # Initial state: inactive
    assert not is_kill_switch_active(tmp_path)

    # 1. Trigger via file
    trigger_kill_switch("Risk threshold exceeded", data_dir=tmp_path)
    assert is_kill_switch_active(tmp_path)

    # Remove file
    (tmp_path / "KILL_SWITCH").unlink()
    assert not is_kill_switch_active(tmp_path)

    # 2. Trigger via env var
    os.environ["POLYARB_KILL_SWITCH"] = "1"
    assert is_kill_switch_active(tmp_path)
    del os.environ["POLYARB_KILL_SWITCH"]
    assert not is_kill_switch_active(tmp_path)


def test_scanner_abnormal_edge_circuit_breaker(tmp_path: Path):
    db = _db()
    cfg = Config()
    cfg.risk.max_anomaly_edge = 0.35  # edges > 35c rejected

    # Huge artificial edge: 0.10 + 0.10 = 0.20 cost -> 0.80 edge!
    markets = {
        "m1": {"id": "m1", "question": "M1", "venue": "polymarket", "event": "ev1"},
        "m2": {"id": "m2", "question": "M2", "venue": "polymarket", "event": "ev1"},
    }
    rel = [{"id": "r1", "kind": "implies", "markets": ["m1", "m2"], "approved": True}]

    def fake_fetch(m):
        return {"YES": [Level(0.10, 50)], "NO": [Level(0.10, 50)]}

    found = scanner.scan_once(rel, markets, db, cfg=cfg, latency=0.0, fetch=fake_fetch)
    assert found == 0  # Rejected by circuit breaker!


def test_scanner_approval_expiration(tmp_path: Path):
    db = _db()
    cfg = Config()
    markets = {
        "m1": {"id": "m1", "question": "M1", "venue": "polymarket", "event": "ev1"},
        "m2": {"id": "m2", "question": "M2", "venue": "polymarket", "event": "ev1"},
    }
    # Expired relation (expires_at in the past)
    rel = [{
        "id": "r1",
        "kind": "implies",
        "markets": ["m1", "m2"],
        "approved": True,
        "expires_at": "2020-01-01T00:00:00Z",
    }]

    def fake_fetch(m):
        return {"YES": [Level(0.45, 50)], "NO": [Level(0.45, 50)]}

    found = scanner.scan_once(rel, markets, db, cfg=cfg, latency=0.0, fetch=fake_fetch)
    assert found == 0  # skipped due to expired approval


def test_scanner_event_exposure_limit(tmp_path: Path):
    db = _db()
    cfg = Config()
    cfg.risk.max_capital_per_event = 50.0  # max $50 per event
    markets = {
        "m1": {"id": "m1", "question": "M1", "venue": "polymarket", "event": "ev1"},
        "m2": {"id": "m2", "question": "M2", "venue": "polymarket", "event": "ev1"},
    }
    rel = [{"id": "r1", "kind": "implies", "markets": ["m1", "m2"], "approved": True}]

    def fake_fetch(m):
        return {"YES": [Level(0.50, 100)], "NO": [Level(0.45, 100)]}

    # Already $50 deployed in event 'ev1'
    exposure = {"event:ev1": 50.0}
    found = scanner.scan_once(rel, markets, db, cfg=cfg, latency=0.0, fetch=fake_fetch, exposure_tracker=exposure)
    assert found == 0  # skipped due to capital cap reached


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        p = Path(tmp)
        test_kill_switch_env_and_file(p)
        print("ok test_kill_switch_env_and_file")
        test_scanner_abnormal_edge_circuit_breaker(p)
        print("ok test_scanner_abnormal_edge_circuit_breaker")
        test_scanner_approval_expiration(p)
        print("ok test_scanner_approval_expiration")
        test_scanner_event_exposure_limit(p)
        print("ok test_scanner_event_exposure_limit")
