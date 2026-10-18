"""Unified test runner for all polyarb test suites."""
from __future__ import annotations
import sys, runpy
from pathlib import Path

test_files = [
    "tests/test_core.py",
    "tests/test_risk_and_payoff.py",
    "tests/test_matcher_polarity.py",
    "tests/test_replay_and_legging.py",
    "tests/test_limits_and_controls.py",
    "tests/test_fixtures.py",
    "tests/test_simulator.py",
    "tests/test_credentials.py",
    "tests/test_audit.py",
    "tests/test_executors.py",
    "tests/test_per_venue_balance.py",
    "tests/test_poller_edge_gate.py",
    "tests/test_orchestrator.py",
    "tests/test_approval_gate.py",
    "tests/test_reconciliation.py",
    "tests/test_fx.py",
    "tests/test_allocator.py",
    "tests/test_optimizer.py",
    "tests/test_bracket_matcher.py",
    "tests/test_kalshi_ws.py",
    "tests/test_manifold.py",
    "tests/test_settlement.py",
    "tests/test_costs.py",
    "tests/test_analytics.py",
    "tests/test_server.py",
    "tests/test_hardening.py",
    "tests/test_sprint5.py",
]

if __name__ == "__main__":
    sys.path.insert(0, ".")
    passed = 0
    total = len(test_files)
    print("=" * 60)
    print("Running all polyarb production test suites")
    print("=" * 60)

    for tf in test_files:
        print(f"\n--- Running {tf} ---")
        try:
            runpy.run_path(tf, run_name="__main__")
            passed += 1
            print(f"[PASS] {tf}")
        except Exception as e:
            print(f"[FAIL] {tf}: {e}")
            sys.exit(1)

    print("\n" + "=" * 60)
    print(f"ALL TESTS PASSED: {passed}/{total} test suites green (100%)")
    print("=" * 60)
