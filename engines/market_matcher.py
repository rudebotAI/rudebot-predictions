"""
Cross-venue market matcher.

Pairs a Kalshi market with a Polymarket market ONLY when we are highly
confident they resolve on the same real-world question. A false match
creates a fake "edge" and loses real money, so the bias is heavily toward
*refusing* to match. Anything ambiguous returns no match (no signal).

Matching gates (all must pass):
  1. Resolution/close dates within CLOSE_TOLERANCE_DAYS.
  2. Normalized-title token similarity >= MIN_SIM.
  3. Every salient token (numbers, % thresholds, proper-noun-ish caps,
     comparison direction words) present in one title is present in the
     other -- this is what stops "Fed cuts to 4.0%" matching "Fed cuts to 4.5%".
  4. Both sides priced and within sane bounds.
"""
import re
from datetime import datetime, timezone
from typing import Optional

CLOSE_TOLERANCE_DAYS = 2.0
MIN_SIM = 0.62            # token Jaccard on normalized titles
MAX_IMPLIED_EDGE = 0.25  # refuse "edges" larger than this -- almost always a bad match

_STOP = {
    "the","a","an","of","to","in","on","for","and","or","will","be","is","are",
    "by","at","this","that","than","be","as","with","it","its","'s","s","market",
}
# light synonym normalization so equivalent phrasings collapse together
_SYN = {
    "federal":"fed","reserve":"fed","fomc":"fed",
    "decrease":"cut","decreases":"cut","cuts":"cut","lower":"cut","reduce":"cut",
    "increase":"hike","increases":"hike","hikes":"hike","raise":"hike",
    "president":"potus","presidential":"potus",
    "republican":"gop",
    "bitcoin":"btc","ethereum":"eth",
    "percent":"%","pct":"%",
}
_DIRECTION = {"above","below","over","under","more","less","cut","hike","up","down",
              "win","lose","beat","yes","no",">","<"}

def _norm_tokens(title: str):
    t = title.lower()
    t = t.replace("%", " % ")
    t = re.sub(r"[^a-z0-9%\.\s\-]", " ", t)
    raw = [w for w in re.split(r"[\s\-]+", t) if w]
    out = []
    for w in raw:
        w = _SYN.get(w, w)
        if w in _STOP:
            continue
        out.append(w)
    return out

_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")

def _salient(tokens):
    """Tokens that MUST match exactly across venues: numbers/thresholds and
    direction words. These encode the actual contract terms."""
    sal = set()
    for w in tokens:
        if _NUM_RE.fullmatch(w) or w in _DIRECTION or w == "%":
            sal.add(w)
    return sal


_CAP_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]{2,})\b")
def _entities(original_title: str):
    """Proper-noun-ish tokens (capitalized words) that identify WHO/WHAT the
    contract is about. These must match across venues -- this is what stops
    'Biden wins Iowa' matching 'Trump wins Iowa'. Synonym-normalized; month
    names and generic stop-caps are dropped."""
    months = {"january","february","march","april","may","june","july","august",
              "september","october","november","december"}
    ents = set()
    for w in _CAP_RE.findall(original_title or ""):
        lw = _SYN.get(w.lower(), w.lower())
        if lw in _STOP or lw in months or lw == "will":
            continue
        ents.add(lw)
    return ents

def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None

def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def match_score(kalshi_mkt: dict, poly_mkt: dict) -> Optional[dict]:
    """Return a match dict if confident, else None."""
    k_title = kalshi_mkt.get("question") or kalshi_mkt.get("title") or ""
    p_title = poly_mkt.get("question") or poly_mkt.get("title") or ""
    if not k_title or not p_title:
        return None

    # Gate 1: resolution dates close together
    kd, pd = _parse_dt(kalshi_mkt.get("end_date")), _parse_dt(poly_mkt.get("end_date"))
    if kd and pd and abs((kd - pd).total_seconds()) / 86400.0 > CLOSE_TOLERANCE_DAYS:
        return None

    kt, pt = _norm_tokens(k_title), _norm_tokens(p_title)
    kset, pset = set(kt), set(pt)

    # Gate 2: overall similarity
    sim = _jaccard(kset, pset)
    if sim < MIN_SIM:
        return None

    # Gate 3: salient terms (numbers/thresholds/direction) must match exactly
    ksal, psal = _salient(kt), _salient(pt)
    if ksal != psal:
        return None

    # Gate 3b: proper-noun entities must match (no Biden<->Trump, Lakers<->Knicks)
    kent, pent = _entities(k_title), _entities(p_title)
    if kent != pent:
        return None

    return {"similarity": round(sim, 3), "salient": sorted(ksal), "entities": sorted(kent)}

def attach_cross_prices(kalshi_markets: list, poly_markets: list, logger=None) -> int:
    """For each Kalshi market, find the single best confident Polymarket match
    and set kalshi_mkt['cross_platform_price'] = poly yes price. Returns count
    of markets enriched. Refuses multi-match ambiguity (>1 strong candidate)."""
    n = 0
    for k in kalshi_markets:
        cands = []
        for p in poly_markets:
            pp = p.get("yes_price")
            if pp is None or not (0.01 <= pp <= 0.99):
                continue
            ms = match_score(k, p)
            if ms:
                cands.append((ms["similarity"], p, ms))
        if not cands:
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        # ambiguity guard: if the top two are both strong & close, refuse
        if len(cands) >= 2 and cands[0][0] - cands[1][0] < 0.05:
            if logger:
                logger.debug("matcher: ambiguous match for %r -- skipping",
                             k.get("question", "")[:60])
            continue
        sim, p, ms = cands[0]
        edge = abs(p["yes_price"] - k.get("yes_price", p["yes_price"]))
        if edge > MAX_IMPLIED_EDGE:
            if logger:
                logger.info("matcher: rejecting implausible edge %.2f on %r (likely bad match)",
                            edge, k.get("question", "")[:60])
            continue
        k["cross_platform_price"] = round(p["yes_price"], 4)
        k["cross_venue"] = "polymarket"
        k["cross_match_sim"] = sim
        k["cross_question"] = p.get("question") or p.get("title")
        n += 1
    return n
