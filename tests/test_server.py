"""Integration tests for PolyArb Dashboard HTTP Server & API endpoints (Phase 5).
Verifies static asset serving (HTML, CSS, JS), analytics API, approvals queue with notes,
and portfolio mark-to-market data delivery.
"""
from __future__ import annotations
import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, ".")
from polyarb.server import DashboardHandler, portfolio, poller, analytics_engine


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def test_server_http_and_api():
    port = get_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. Test GET / (HTML template / static index)
        with urllib.request.urlopen(f"{base_url}/") as resp:
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "text/html" in ct
            body = resp.read().decode("utf-8")
            assert "POLYARB" in body or "PolyArb" in body
            assert "positions-list" in body

        # 2. Test GET /static/app.css
        with urllib.request.urlopen(f"{base_url}/static/app.css") as resp:
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "text/css" in ct
            css_body = resp.read().decode("utf-8")
            assert "--card-bg" in css_body
            assert "rules-diff-grid" in css_body

        # 3. Test GET /static/app.js
        with urllib.request.urlopen(f"{base_url}/static/app.js") as resp:
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "javascript" in ct
            js_body = resp.read().decode("utf-8")
            assert "renderSvgEquityCurve" in js_body
            assert "renderApprovals" in js_body

        # 4. Test GET /api/portfolio
        with urllib.request.urlopen(f"{base_url}/api/portfolio") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "summary" in data
            assert "poller" in data
            assert "open_structures" in data
            assert "realised_structures" in data
            assert data["summary"]["initial_capital_inr"] == 10000.0

        # 5. Test GET /api/analytics
        with urllib.request.urlopen(f"{base_url}/api/analytics") as resp:
            assert resp.status == 200
            adata = json.loads(resp.read().decode("utf-8"))
            assert "equity_curve" in adata
            assert "metrics" in adata
            assert "sharpe_ratio" in adata["metrics"]
            assert "max_drawdown_pct" in adata["metrics"]
            assert "win_rate_pct" in adata["metrics"]
            assert "edge_decay" in adata
            assert "breakdowns" in adata
            assert "profit_model" in adata

        # 6. Test GET /api/approvals
        with urllib.request.urlopen(f"{base_url}/api/approvals") as resp:
            assert resp.status == 200
            app_data = json.loads(resp.read().decode("utf-8"))
            assert "mode" in app_data
            assert "pending" in app_data
            assert "history" in app_data

        # 7. Test POST /api/approvals/mode
        req = urllib.request.Request(
            f"{base_url}/api/approvals/mode",
            data=json.dumps({"mode": "HUMAN_APPROVAL"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            m_res = json.loads(resp.read().decode("utf-8"))
            assert m_res["status"] == "success"
            assert poller.approval_gate.mode == "HUMAN_APPROVAL"

        # 8. Test candidate queuing and approval with operator notes
        cand_item = poller.approval_gate.submit_for_approval(
            relation_id="rel_server_test",
            kind="bracket",
            event_title="CPI YoY Rate Ladder",
            legs=[
                {
                    "market_id": "poly_cpi_1",
                    "venue": "polymarket",
                    "question": "CPI >= 3.0%",
                    "side": "YES",
                    "entry_price_usd": 0.40,
                    "shares": 10.0,
                    "resolution_source": "UMA Oracle (Polygon)",
                    "rules_summary": "Bureau of Labor Statistics CPI-U report",
                },
                {
                    "market_id": "kalshi_cpi_1",
                    "venue": "kalshi",
                    "question": "CPI >= 3.0%",
                    "side": "NO",
                    "entry_price_usd": 0.55,
                    "shares": 10.0,
                    "resolution_source": "CFTC Official Settlement",
                    "rules_summary": "BLS CPI-U 12-month percentage change",
                },
            ],
            unit_cost=0.95,
            expected_payoff=1.00,
            net_edge=0.05,
            shares=10.0,
        )

        # Check pending approvals has remaining TTL and rules
        with urllib.request.urlopen(f"{base_url}/api/approvals") as resp:
            app_data = json.loads(resp.read().decode("utf-8"))
            assert len(app_data["pending"]) >= 1
            item = next(i for i in app_data["pending"] if i["id"] == cand_item.id)
            assert item["ttl_remaining_sec"] > 0
            assert len(item["legs"]) == 2
            assert item["legs"][0]["resolution_source"] == "UMA Oracle (Polygon)"

        # Approve with operator notes
        appr_req = urllib.request.Request(
            f"{base_url}/api/approvals/approve",
            data=json.dumps({
                "item_id": cand_item.id,
                "approved_by": "lead_trader",
                "notes": "Verified CPI definition concordant across BLS release.",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(appr_req) as resp:
            assert resp.status == 200
            appr_res = json.loads(resp.read().decode("utf-8"))
            assert appr_res["status"] == "success"

        # Verify item in history has operator notes
        history_item = poller.approval_gate.items[cand_item.id]
        assert history_item.operator_notes == "Verified CPI definition concordant across BLS release."
        assert history_item.approved_by == "lead_trader"

        # Switch back to PAPER mode
        poller.approval_gate.set_mode("PAPER")
        print("ok test_server_http_and_api")

    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    test_server_http_and_api()
    print("\nALL SERVER TESTS PASSED!")
