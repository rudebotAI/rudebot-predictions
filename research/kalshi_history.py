"""
Kalshi settled-market history — the free, unauthenticated data layer behind
the Research stage.

Pulls settled markets (with their series category), the hourly candlestick
series for each market's life, and derives the mid price at fixed horizons
before close. Cached as JSONL under research/cache/ so a re-run is cheap.

Everything here uses public endpoints (no API key):
    GET /series                    -> category per series ticker
    GET /markets?status=settled    -> result, open/close time, volume
    GET /series/{s}/markets/{t}/candlesticks?period_interval=60
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# Horizons (hours before close) at which the calibration is estimated.
HORIZONS_H = (1, 6, 24, 72, 168)

# Series categories worth modelling. "Exotics" (parlays) are excluded on
# purpose: they are 2x-fee combos the bot never trades.
CATEGORIES = (
    "Sports", "Politics", "Elections", "Economics", "Financials", "Crypto",
    "Climate and Weather", "Entertainment", "Science and Technology",
    "Companies", "World", "Commodities", "Mentions", "Health",
)

_MIN_INTERVAL = 0.06   # ~16 req/s, under the 20/s public read bucket
_last_call = 0.0


def _get(path: str, retries: int = 4) -> dict:
    global _last_call
    for attempt in range(retries):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(KALSHI_API + path, headers={"User-Agent": "rudebot-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                _last_call = time.monotonic()
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"kalshi GET failed after {retries} tries: {path}")


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---- series -> category -------------------------------------------------------

def series_catalog(use_cache: bool = True) -> dict[str, dict]:
    """{series_ticker: {category, frequency, fee_type, fee_multiplier}} for
    every series on the exchange (~14k as of Sept 2026)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / "series.json"
    if use_cache and p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
        return json.loads(p.read_text())
    out: dict[str, dict] = {}
    cursor = ""
    while True:
        d = _get("/series?limit=200" + (f"&cursor={cursor}" if cursor else ""))
        for s in d.get("series", []):
            out[s["ticker"]] = {"category": s.get("category"), "frequency": s.get("frequency"),
                                "fee_type": s.get("fee_type"), "fee_multiplier": s.get("fee_multiplier")}
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    p.write_text(json.dumps(out))
    return out


# ---- settled markets ----------------------------------------------------------

def settled_markets(series_ticker: str, max_markets: int = 200, min_volume: float = 20.0,
                    since_ts: int | None = None) -> list[dict]:
    """Settled markets for one series, newest first, filtered to real volume."""
    out: list[dict] = []
    cursor = ""
    while len(out) < max_markets:
        q = f"/markets?status=settled&series_ticker={urllib.parse.quote(series_ticker)}&limit=200"
        if since_ts:
            q += f"&min_close_ts={since_ts}"
        if cursor:
            q += f"&cursor={cursor}"
        d = _get(q)
        ms = d.get("markets", [])
        for m in ms:
            vol = _f(m.get("volume_fp"), None)
            if vol is None:
                vol = _f(m.get("volume"), 0.0)
            if vol < min_volume or m.get("result") not in ("yes", "no"):
                continue
            out.append({
                "ticker": m["ticker"], "event_ticker": m.get("event_ticker"),
                "series": series_ticker, "result": m["result"],
                "open_ts": _ts(m.get("open_time")), "close_ts": _ts(m.get("close_time")),
                # Scheduled resolution. close_time is REWRITTEN to the moment
                # trading stopped when a market closes early, so horizons must
                # be measured from this, never from close_time.
                "sched_ts": _ts(m.get("expected_expiration_time")) or _ts(m.get("close_time")),
                "settlement_ts": _ts(m.get("settlement_ts")), "volume": vol,
                "strike_type": m.get("strike_type"),
                "can_close_early": bool(m.get("can_close_early")),
            })
        cursor = d.get("cursor") or ""
        if not cursor or not ms:
            break
    return out[:max_markets]


# ---- candlesticks -> price at horizon --------------------------------------------

def candles(series_ticker: str, ticker: str, start_ts: int, end_ts: int, interval: int = 60) -> list[dict]:
    """Hourly candles for one market. Kalshi caps the window; page if needed."""
    out: list[dict] = []
    cur = start_ts
    max_span = 5000 * interval * 60           # server cap: 5000 periods per call
    while cur < end_ts:
        stop = min(end_ts, cur + max_span)
        d = _get(f"/series/{series_ticker}/markets/{ticker}/candlesticks"
                 f"?start_ts={cur}&end_ts={stop}&period_interval={interval}")
        cs = d.get("candlesticks", [])
        out.extend(cs)
        if stop >= end_ts:
            break
        cur = stop
    return out


def _mid_from_candle(c: dict) -> float | None:
    """Mid of close bid/ask if both quoted, else last trade close, else None.
    A one-sided book (bid 0 or ask 1) is treated as no quote."""
    bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
    ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
    if bid is not None and ask is not None and 0.0 < bid < 1.0 and 0.0 < ask < 1.0 and ask >= bid:
        return (bid + ask) / 2.0
    px = _f((c.get("price") or {}).get("close_dollars"))
    if px is not None and 0.0 < px < 1.0:
        return px
    return None


STILL_TRADING_MARGIN_S = 600


def price_at_horizons(cs: list[dict], sched_ts: int, close_ts: int | None = None,
                      horizons_h=HORIZONS_H) -> dict[int, float]:
    """For each horizon h (hours before the SCHEDULED resolution) the last
    quoted mid at or before sched_ts - h*3600, but only if the market was
    still trading for STILL_TRADING_MARGIN_S after that cutoff (an early-closed
    market has no live price after the event — using its final candle would
    be lookahead). Candles are keyed by end_period_ts."""
    if not cs:
        return {}
    close_ts = close_ts or sched_ts
    pts = [(int(c.get("end_period_ts", 0)), _mid_from_candle(c)) for c in cs]
    pts = [(t, p) for t, p in pts if p is not None and t > 0]
    pts.sort()
    out: dict[int, float] = {}
    for h in horizons_h:
        cutoff = sched_ts - h * 3600
        if cutoff > close_ts - STILL_TRADING_MARGIN_S:
            continue
        last = None
        for t, p in pts:
            if t <= cutoff:
                last = p
            else:
                break
        if last is not None:
            out[h] = last
    return out


# ---- build the dataset ------------------------------------------------------------

def build_dataset(categories=CATEGORIES, days_back: int = 60, max_markets_per_series: int = 60,
                  max_series_per_category: int = 40, min_volume: float = 20.0,
                  out_path: Path | None = None, log_every: int = 25) -> Path:
    """Write research/cache/settled_<date>.jsonl with one row per settled
    market: category, result, volume, horizon prices. Bounded so a full run
    stays within ~30 minutes at the public rate limit."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out_path or (CACHE_DIR / f"settled_{datetime.now(timezone.utc):%Y%m%d}.jsonl")
    cat = series_catalog()
    since = int(time.time()) - days_back * 86400
    by_cat: dict[str, list[str]] = {}
    for s, info in cat.items():
        c = info.get("category")
        if c in categories and not s.startswith("KXMVE"):
            by_cat.setdefault(c, []).append(s)
    n_rows = 0
    with out_path.open("w") as fh:
        for c, tickers in by_cat.items():
            rows_c = 0
            series_seen = 0
            for s in tickers:
                if series_seen >= max_series_per_category:
                    break
                try:
                    ms = settled_markets(s, max_markets=max_markets_per_series, min_volume=min_volume, since_ts=since)
                except Exception as e:  # noqa: BLE001
                    logger.warning("settled_markets failed %s: %s", s, e)
                    continue
                if not ms:
                    continue
                series_seen += 1
                for m in ms:
                    if not m["open_ts"] or not m["close_ts"] or m["close_ts"] <= m["open_ts"]:
                        continue
                    try:
                        cs = candles(s, m["ticker"], m["open_ts"], m["close_ts"])
                    except Exception as e:  # noqa: BLE001
                        logger.warning("candles failed %s: %s", m["ticker"], e)
                        continue
                    hp = price_at_horizons(cs, m["sched_ts"], m["close_ts"])
                    if not hp:
                        continue
                    row = {**m, "category": c, "frequency": cat[s].get("frequency"),
                           "life_h": round((m["close_ts"] - m["open_ts"]) / 3600, 1),
                           "early_close": bool(m["sched_ts"] - m["close_ts"] > 2 * 3600),
                           "y": 1 if m["result"] == "yes" else 0,
                           "p": {str(h): round(v, 4) for h, v in hp.items()}}
                    fh.write(json.dumps(row) + "\n")
                    n_rows += 1
                    rows_c += 1
                    if n_rows % log_every == 0:
                        logger.info("dataset: %d rows (%s: %d)", n_rows, c, rows_c)
            logger.info("category %s: %d rows from %d series", c, rows_c, series_seen)
    logger.info("dataset written: %s (%d rows)", out_path, n_rows)
    return out_path


def load_dataset(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--per-series", type=int, default=60)
    ap.add_argument("--series-per-category", type=int, default=40)
    ap.add_argument("--min-volume", type=float, default=20.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    build_dataset(days_back=a.days, max_markets_per_series=a.per_series,
                  max_series_per_category=a.series_per_category, min_volume=a.min_volume,
                  out_path=Path(a.out) if a.out else None)
