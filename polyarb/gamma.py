"""Market discovery + candidate relation generation (stdlib only).
NOTE: field names follow Polymarket's public Gamma/CLOB docs; verify against live
responses on first run."""
from __future__ import annotations
import hashlib, json, os, re, urllib.parse, urllib.request
from collections import defaultdict
from typing import Any

from .core import Level
from .transport import http_get

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def fetch_events(limit: int = 100, max_pages: int = 10) -> list[dict]:
    out = []
    for p in range(max_pages):
        batch = http_get(f"{GAMMA}/events", {"active": "true", "closed": "false",
                                             "limit": limit, "offset": p * limit})
        out += batch
        if len(batch) < limit:
            break
    return out


def _j(x: Any) -> Any:
    return json.loads(x) if isinstance(x, str) else x


def parse_market(m: dict, event: dict) -> dict | None:
    try:
        outcomes, tokens = _j(m.get("outcomes")), _j(m.get("clobTokenIds"))
        if [o.lower() for o in outcomes] != ["yes", "no"] or len(tokens) != 2:
            return None
        if m.get("enableOrderBook") is False or not m.get("conditionId") or m.get("closed") is True:
            return None
    except Exception:
        return None

    rules = (m.get("description") or m.get("resolutionCriteria") or
             event.get("description") or event.get("resolutionCriteria") or "")
    source = (m.get("resolutionSource") or event.get("resolutionSource") or "")

    return {
        "venue": "polymarket",
        "id": m["conditionId"],
        "question": m.get("question", ""),
        "yes": tokens[0],
        "no": tokens[1],
        "end": m.get("endDate"),
        "neg_risk": bool(event.get("negRisk")),
        "event": event.get("slug"),
        "event_title": event.get("title"),
        "resolution_rules": rules,
        "resolution_source": source,
        "cutoff_time": m.get("endDate") or "",
    }


def load_markets(events: list[dict]) -> dict:
    out = {}
    for ev in events:
        for m in ev.get("markets", []):
            pm = parse_market(m, ev)
            if pm:
                out[pm["id"]] = pm
    return out


_MONTHS = r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?"


def normalise(q: str) -> str:
    q = q.lower()
    q = re.sub(rf"\b({_MONTHS})\b", " ", q)
    q = re.sub(r"\d+(st|nd|rd|th)?", " ", q)
    q = re.sub(r"[^a-z ]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _rel(kind: str, ms: list[dict], note: str) -> dict:
    ids = [m["id"] for m in ms]
    rid = hashlib.sha1((kind + "|" + "|".join(ids)).encode()).hexdigest()[:10]
    return {
        "id": rid,
        "kind": kind,
        "markets": ids,
        "questions": [m.get("question", "") for m in ms],
        "note": note,
        "approved": False,
        "approved_by": None,
        "approved_at": None,
        "expires_at": None,
    }


def candidates(markets: dict) -> list[dict]:
    """PROPOSALS ONLY. A human must read both resolution rules and set approved=true."""
    by_event = defaultdict(list)
    for m in markets.values():
        by_event[m["event"]].append(m)
    out = []
    for ms in by_event.values():
        groups = defaultdict(list)
        for m in ms:
            if m.get("end"):
                groups[normalise(m.get("question", ""))].append(m)
        for g in groups.values():           # date ladders: earlier deadline => later deadline
            g.sort(key=lambda m: m["end"])
            for a, b in zip(g, g[1:]):
                if a["end"] != b["end"]:
                    out.append(_rel("implies", [a, b], "date ladder; confirm both say 'by <date>'"))
        if len(ms) >= 2 and ms[0].get("neg_risk"):
            out.append(_rel("exclusive", ms, "neg-risk event; check for 'Other'/unlisted outcomes"))
    return out


def fetch_asks(token_id: str) -> list[Level]:
    d = http_get(f"{CLOB}/book", {"token_id": token_id})
    return sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("asks", [])),
                  key=lambda l: l.price)


def fetch_book(token_id: str) -> dict[str, list[Level]]:
    """Returns both bids and asks for depth analysis and unwind estimation."""
    d = http_get(f"{CLOB}/book", {"token_id": token_id})
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("asks", [])),
                  key=lambda l: l.price)
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("bids", [])),
                  key=lambda l: l.price, reverse=True)
    return {"asks": asks, "bids": bids}
