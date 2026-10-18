import argparse, json, sys
from pathlib import Path
import sqlite3
from . import gamma, kalshi, matcher, scanner, recorder, stream, doctor, replay as rp
from .config import Config, trigger_kill_switch, is_kill_switch_active

ap = argparse.ArgumentParser(prog="polyarb")
sub = ap.add_subparsers(dest="cmd", required=True)

# 1. discover
dsc = sub.add_parser("discover", help="fetch markets, propose relations and threshold ladders")
dsc.add_argument("--kalshi", action="store_true", help="also load Kalshi + cross-venue matches")

# 2. approve
appr = sub.add_parser("approve", help="approve a relation with audit trail and expiration")
appr.add_argument("relation_id", help="the 10-character relation ID to approve")
appr.add_argument("--user", default="human", help="approver name")
appr.add_argument("--days", type=int, default=30, help="days until approval expires")

# 3. candidates
cnd = sub.add_parser("candidates", help="list relations with approval status and risk flags")
cnd.add_argument("--approved-only", action="store_true")
cnd.add_argument("--unapproved-only", action="store_true")

# 4. scan
s = sub.add_parser("scan", help="paper-scan approved relations with risk caps")
s.add_argument("--interval", type=float, default=30)
s.add_argument("--fee-bps", type=float, default=0.0)
s.add_argument("--min-edge", type=float, default=0.005)
s.add_argument("--min-shares", type=float, default=5)
s.add_argument("--latency", type=float, default=0.5)

# 5. record
r = sub.add_parser("record", help="record order books for approved relations")
r.add_argument("--interval", type=float, default=5)
r.add_argument("--depth", type=int, default=10)
r.add_argument("--workers", type=int, default=8)
r.add_argument("--all-candidates", action="store_true", help="record unapproved relations too")

# 6. stream
st = sub.add_parser("stream", help="websocket recorder (Polymarket) + Kalshi polling")
st.add_argument("--depth", type=int, default=10)
st.add_argument("--flush", type=float, default=0.1, help="seconds between DB flushes")
st.add_argument("--chunk", type=int, default=100, help="markets per websocket connection")
st.add_argument("--kalshi-interval", type=float, default=5)
st.add_argument("--all-candidates", action="store_true")

# 7. doctor
sub.add_parser("doctor", help="verify live API field names (first run)")

# 8. replay
p = sub.add_parser("replay", help="replay recorded books, report edge persistence and legging")
p.add_argument("--horizons", default="5,15,60")
p.add_argument("--fee-bps", type=float, default=0.0)
p.add_argument("--min-edge", type=float, default=0.005)
p.add_argument("--min-shares", type=float, default=5)
p.add_argument("--min-apy", type=float, default=0.0, help="minimum annualized return (e.g. 0.15 for 15%)")
p.add_argument("--exec-delay", type=float, default=0.25, help="simulated execution delay between legs (seconds)")
p.add_argument("--no-phantom-filter", action="store_true", help="disable phantom-arb multi-tick filter")
p.add_argument("--all-candidates", action="store_true")

# 9. kill / unkill
k = sub.add_parser("kill", help="emergency halt: trigger kill switch")
k.add_argument("--reason", default="Manual operator kill switch trigger")
sub.add_parser("unkill", help="remove kill switch and allow running")

a = ap.parse_args()

d = Path("data")
d.mkdir(exist_ok=True)

if a.cmd == "discover":
    poly = gamma.load_markets(gamma.fetch_events())
    markets = dict(poly)
    new = gamma.candidates(poly) + matcher.find_threshold_ladders(poly)

    if a.kalshi:
        kal = kalshi.load_markets(kalshi.fetch_events())
        markets.update(kal)
        new += gamma.candidates(kal) + matcher.find_threshold_ladders(kal) + matcher.match(poly, kal)

    # Deduplicate candidate relations by ID
    dedup = {}
    for r in new:
        dedup[r["id"]] = r
    new = list(dedup.values())

    (d / "markets.json").write_text(json.dumps(markets, indent=2))
    cf = d / "candidates.json"
    old = {r["id"]: r for r in json.loads(cf.read_text())} if cf.exists() else {}

    for r in new:
        if r["id"] in old:
            # Preserve human decisions and approval metadata
            r["approved"] = old[r["id"]].get("approved", False)
            r["approved_by"] = old[r["id"]].get("approved_by")
            r["approved_at"] = old[r["id"]].get("approved_at")
            r["expires_at"] = old[r["id"]].get("expires_at")

    cf.write_text(json.dumps(new, indent=2))
    print(f"{len(markets)} markets, {len(new)} candidate relations -> data/candidates.json")

elif a.cmd == "approve":
    cf = d / "candidates.json"
    if not cf.exists():
        print("data/candidates.json not found. Run 'python -m polyarb discover' first.")
        sys.exit(1)
    rels = json.loads(cf.read_text(encoding="utf-8"))
    found = False
    for r in rels:
        if r["id"] == a.relation_id:
            matcher.approve_relation(r, approved_by=a.user, expiry_days=a.days)
            found = True
            print(f"APPROVED relation {r['id']} ({r['kind']}) by {a.user} for {a.days} days.")
            print(f"Questions:\n  " + "\n  ".join(r["questions"]))
            if r.get("risk_flags"):
                print(f"Risk warnings: {', '.join(r['risk_flags'])}")
            break
    if not found:
        print(f"Relation ID {a.relation_id} not found in data/candidates.json")
        sys.exit(1)
    cf.write_text(json.dumps(rels, indent=2), encoding="utf-8")

elif a.cmd == "candidates":
    cf = d / "candidates.json"
    if not cf.exists():
        print("data/candidates.json not found.")
        sys.exit(1)
    rels = json.loads(cf.read_text(encoding="utf-8"))
    for r in rels:
        app = r.get("approved", False)
        if a.approved_only and not app:
            continue
        if a.unapproved_only and app:
            continue
        flags = f" [FLAGS: {', '.join(r['risk_flags'])}]" if r.get("risk_flags") else ""
        exp = f" (expires {r['expires_at'][:10]})" if r.get("expires_at") else ""
        status = f"APPROVED by {r.get('approved_by')}{exp}" if app else "PENDING"
        print(f"[{r['id']}] [{status}] {r['kind']}: {' vs '.join(r['questions'])}{flags}")

elif a.cmd == "kill":
    trigger_kill_switch(a.reason, data_dir=d)
    print(f"Kill switch activated. Reason: {a.reason}")

elif a.cmd == "unkill":
    kp = d / "KILL_SWITCH"
    if kp.exists():
        kp.unlink()
        print("Removed data/KILL_SWITCH. Operations may resume.")
    else:
        print("Kill switch file was not present.")

elif a.cmd == "record":
    recorder.run(interval=a.interval, depth=a.depth, workers=a.workers,
                 all_candidates=a.all_candidates)

elif a.cmd == "doctor":
    raise SystemExit(doctor.main())

elif a.cmd == "stream":
    stream.run(depth=a.depth, flush=a.flush, chunk=a.chunk,
               kalshi_interval=a.kalshi_interval, all_candidates=a.all_candidates)

elif a.cmd == "replay":
    markets = json.loads((d / "markets.json").read_text(encoding="utf-8"))
    rels = json.loads((d / "candidates.json").read_text(encoding="utf-8"))
    if not a.all_candidates:
        rels = [x for x in rels if x.get("approved")]
    hs = tuple(int(x) for x in a.horizons.split(","))
    eps = rp.replay(
        sqlite3.connect(d / "books.sqlite"),
        rels,
        markets,
        horizons=hs,
        fee_bps=a.fee_bps,
        min_edge=a.min_edge,
        min_shares=a.min_shares,
        filter_phantom=not a.no_phantom_filter,
        exec_delay=a.exec_delay,
        min_apy=a.min_apy,
    )
    for k, v in rp.summarize(eps, hs).items():
        print(f"{k:>30}: {v}")

else:
    scanner.run(interval=a.interval, fee_bps=a.fee_bps, min_edge=a.min_edge,
                min_shares=a.min_shares, latency=a.latency)
