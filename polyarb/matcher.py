"""Cross-venue market matching, polarity checks, resolution risk analysis, and threshold ladders.

PROPOSALS ONLY: All candidates require human approval.
Guards:
- Close expiry dates
- Identical numeric thresholds
- Polarity detection (win/lose, above/below, not)
- Resolution source and cutoff mismatch auto-flagging
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import datetime, timezone

from .gamma import _rel

STOP = set("the a an of in on by at to for will be is are and or if than this that "
           "before after during".split())

NEG_WORDS = set("not no never fail fails failed lose loses lost below under sub "
                "less drop fall fallen".split())
POS_WORDS = set("win wins won pass passes passed above over exceed exceeds "
                "reach reaches reached hit hits at_least".split())


ENTITY_ALIASES = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "fed": "federal reserve",
    "cpi": "inflation",
    "gdp": "gross domestic product",
    "ecb": "european central bank",
    "boe": "bank of england",
}

REVERSE_ALIASES = {v: k for k, v in ENTITY_ALIASES.items()}


def normalize_entity_name(text: str) -> str:
    """Normalizes financial tickers, political names, and macroeconomic abbreviations (Phase 3.5)."""
    clean = text.lower().strip()
    words = clean.split()
    norm = [ENTITY_ALIASES.get(w, w) for w in words]
    return " ".join(norm)


def toks(q: str) -> set[str]:
    raw_tokens = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOP and not w.isdigit()}
    expanded = set(raw_tokens)
    for w in raw_tokens:
        if w in ENTITY_ALIASES:
            for part in ENTITY_ALIASES[w].split():
                if part not in STOP:
                    expanded.add(part)
        if w in REVERSE_ALIASES:
            expanded.add(REVERSE_ALIASES[w])
    return expanded


def nums(q: str) -> set[str]:
    n = set(re.findall(r"\d+(?:\.\d+)?", q.replace(",", "")))
    return {x for x in n if not re.fullmatch(r"(19|20)\d\d", x)}


def day(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).toordinal()
    except Exception:
        return None


def parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def detect_polarity(q: str) -> str:
    """Detects whether question is positively framed, negatively framed, or neutral.
    Returns: 'POS', 'NEG', or 'NEUTRAL'."""
    words = set(re.findall(r"[a-z]+", q.lower()))
    has_neg = bool(words & NEG_WORDS)
    has_pos = bool(words & POS_WORDS)
    if has_neg and not has_pos:
        return "NEG"
    if has_pos and not has_neg:
        return "POS"
    return "NEUTRAL"


def extract_numeric_threshold(q: str) -> tuple[float | None, str | None]:
    """Extract numeric threshold and direction (above/below).
    Example: 'Will BTC be above 100,000 on Dec 31?' -> (100000.0, 'above')"""
    q_clean = q.replace(",", "").lower()
    m_dir = None
    if any(w in q_clean for w in ("above", "over", "exceed", "greater", "higher", ">")):
        m_dir = "above"
    elif any(w in q_clean for w in ("below", "under", "less", "lower", "<")):
        m_dir = "below"

    # Find numbers excluding 4-digit years (2024-2030)
    matches = re.findall(r"\b(\d+(?:\.\d+)?)\b", q_clean)
    candidates = []
    for x in matches:
        val = float(x)
        if not (2020 <= val <= 2035 and "." not in x):
            candidates.append(val)
    if candidates and m_dir:
        return candidates[0], m_dir
    return None, None


def check_resolution_mismatches(m1: dict, m2: dict) -> list[str]:
    """Auto-flag potential resolution risk mismatches between two candidate markets."""
    flags = []

    # 1. Polarity check
    p1, p2 = detect_polarity(m1.get("question", "")), detect_polarity(m2.get("question", ""))
    if p1 != "NEUTRAL" and p2 != "NEUTRAL" and p1 != p2:
        flags.append("RISK_POLARITY_MISMATCH")

    # 2. Cutoff time mismatch
    dt1, dt2 = parse_datetime(m1.get("end")), parse_datetime(m2.get("end"))
    if dt1 and dt2:
        diff_hours = abs((dt1 - dt2).total_seconds()) / 3600.0
        if diff_hours > 24.0:
            flags.append(f"RISK_CUTOFF_MISMATCH_{diff_hours:.0f}H")
        elif diff_hours > 6.0:
            flags.append(f"RISK_CUTOFF_DIFF_{diff_hours:.0f}H")

    # 3. Timing semantics ("by" vs "in")
    q1, q2 = m1.get("question", "").lower(), m2.get("question", "").lower()
    has_by1 = bool(re.search(r"\bby\b", q1))
    has_by2 = bool(re.search(r"\bby\b", q2))
    has_in1 = bool(re.search(r"\bin\b", q1))
    has_in2 = bool(re.search(r"\bin\b", q2))
    if (has_by1 and has_in2 and not has_by2) or (has_by2 and has_in1 and not has_by1):
        flags.append("RISK_BY_VS_IN_SEMANTICS")

    # 4. Resolution source mismatch if recorded
    src1 = (m1.get("resolution_source") or "").strip().lower()
    src2 = (m2.get("resolution_source") or "").strip().lower()
    if src1 and src2 and src1 != src2:
        flags.append("RISK_DIFFERENT_RESOLUTION_SOURCES")

    return flags


def is_approval_expired(rel: dict, now: datetime | None = None) -> bool:
    """Check if a previously approved relation has passed its expiration date."""
    if not rel.get("approved"):
        return False
    exp_str = rel.get("expires_at")
    if not exp_str:
        return False
    dt_exp = parse_datetime(exp_str)
    if not dt_exp:
        return False
    now_dt = now or datetime.now(timezone.utc)
    return now_dt > dt_exp


def approve_relation(rel: dict, approved_by: str = "human", expiry_days: int = 30,
                     now: datetime | None = None) -> dict:
    """Mark relation as approved with full audit trail and expiration date."""
    n = now or datetime.now(timezone.utc)
    exp = n.fromtimestamp(n.timestamp() + expiry_days * 86400, tz=timezone.utc)
    rel["approved"] = True
    rel["approved_by"] = approved_by
    rel["approved_at"] = n.isoformat()
    rel["expires_at"] = exp.isoformat()
    return rel


def find_threshold_ladders(markets: dict) -> list[dict]:
    """Finds numeric threshold ladder relations across markets with the same subject and deadline.
    For 'above': Higher threshold implies Lower threshold (e.g. BTC >= 100k => BTC >= 90k).
    For 'below': Lower threshold implies Higher threshold (e.g. BTC <= 80k => BTC <= 90k)."""
    # Group by normalized subject & deadline day
    groups = defaultdict(list)
    for m in markets.values():
        d = day(m.get("end"))
        thresh, direction = extract_numeric_threshold(m.get("question", ""))
        if d is not None and thresh is not None and direction:
            # normalized base question without the threshold number
            base_q = re.sub(r"\b\d+(?:\.\d+)?\b", " ", m.get("question", "").lower())
            base_q = re.sub(r"\s+", " ", base_q).strip()
            groups[(base_q, d, direction)].append((thresh, m))

    out = []
    for (base_q, d, direction), items in groups.items():
        if len(items) < 2:
            continue
        # sort by threshold
        items.sort(key=lambda x: x[0])
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                th_low, m_low = items[i]
                th_high, m_high = items[j]
                if th_low == th_high:
                    continue
                if direction == "above":
                    # High implies Low (if BTC >= 100k, then BTC >= 90k)
                    rel = _rel("implies", [m_high, m_low],
                               f"threshold ladder: {th_high} >= {th_low}; confirm same index and cutoff")
                else:
                    # Low implies High (if BTC <= 80k, then BTC <= 90k)
                    rel = _rel("implies", [m_low, m_high],
                               f"threshold ladder: {th_low} <= {th_high}; confirm same index and cutoff")
                rel["risk_flags"] = check_resolution_mismatches(m_high, m_low)
                out.append(rel)
    return out


def match(poly: dict, kalshi: dict, max_days=2, min_score=0.5) -> list[dict]:
    bucket = defaultdict(list)
    for k in kalshi.values():
        d = day(k["end"]) if k.get("end") else None
        if d:
            bucket[d].append((k, toks(k["question"]), nums(k["question"])))
    out = []
    for p in poly.values():
        d = day(p["end"]) if p.get("end") else None
        if not d:
            continue
        pt, pn, best = toks(p["question"]), nums(p["question"]), None
        for off in range(-max_days, max_days + 1):
            for k, kt, kn in bucket.get(d + off, []):
                if pn != kn or not pt or not kt:
                    continue
                score = len(pt & kt) / len(pt | kt)
                if score >= min_score and (best is None or score > best[0]):
                    best = (score, k)
        if best:
            score, k_match = best
            flags = check_resolution_mismatches(p, k_match)
            if "RISK_POLARITY_MISMATCH" in flags:
                note = (f"REJECTED/OPPOSITE POLARITY: score {score:.2f}, but opposite phrasing "
                        f"detected ('win' vs 'lose', 'above' vs 'below', or 'not')")
            else:
                note = f"cross-venue match score {score:.2f}; compare resolution sources and cutoff times"

            rel = _rel("equivalent", [p, k_match], note)
            rel["risk_flags"] = flags
            rel["match_score"] = round(score, 3)
            # Store side-by-side rules info for human approval
            rel["markets_info"] = [
                {"id": p["id"], "venue": p.get("venue", "polymarket"), "question": p.get("question"),
                 "end": p.get("end"), "rules": p.get("resolution_rules", ""), "source": p.get("resolution_source", "")},
                {"id": k_match["id"], "venue": k_match.get("venue", "kalshi"), "question": k_match.get("question"),
                 "end": k_match.get("end"), "rules": k_match.get("resolution_rules", ""), "source": k_match.get("resolution_source", "")}
            ]
            out.append(rel)
    return out






def extract_series_slug(market: dict) -> str:
    """Extracts structured series or event identifier before falling back to free text (Phase 3.5)."""
    return (
        market.get("series_ticker") or
        market.get("event_ticker") or
        market.get("ticker_name") or
        market.get("group_slug") or
        ""
    ).lower().strip()


def find_bracket_structures(markets: dict, min_legs: int = 3, max_legs: int = 6) -> list[dict]:
    """Discovers mutually exclusive partition brackets from grouped series markets (Phase 3.4).
    For example: Kalshi rate brackets or Polymarket categorical outcomes."""
    groups = defaultdict(list)
    for m in markets.values():
        slug = extract_series_slug(m)
        if slug:
            groups[slug].append(m)
        else:
            event = (m.get("event") or m.get("event_title") or "").strip().lower()
            if event and len(event) > 4:
                groups[event].append(m)

    out = []
    for slug, mkts in groups.items():
        if min_legs <= len(mkts) <= max_legs:
            # Check if all share the same resolution date (within 1 day)
            end_days = [day(m.get("end")) for m in mkts if m.get("end")]
            if len(end_days) == len(mkts) and max(end_days) - min(end_days) <= 1:
                rel = _rel(
                    "partition",
                    mkts,
                    f"Bracket Partition structure across {len(mkts)} outcomes in series '{slug}'"
                )
                rel["risk_flags"] = ["BRACKET_PARTITION", "MULTI_LEG"]
                out.append(rel)
    return out
