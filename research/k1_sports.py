"""
K1 backtest — sports final-minutes leader (pre-registered in
docs/research/preregistration/K1-sports-final-minutes.md).

Data: public `GET /markets/trades` for settled single-game moneyline markets
(one market per game — the two team markets mirror each other). For every
game we keep the trade tape of the last WINDOW_S seconds before the actual
close and evaluate, at each minute-to-close, the LEADER's last print vs the
outcome.

Two outputs:
1. Reliability of the leader price by (minutes-to-close, price bucket): is
   the leader systematically underpriced in the final minutes (the arXiv
   2607.14430 finding), and by how much?
2. The frozen strategy: at the first observation inside the final
   ENTRY_WINDOW_S seconds where the leader prints >= MIN_LEADER, rest a bid one
   tick below that print. Filled only if a LATER trade prints at or below our
   bid (conservative). Payoff = outcome - bid - maker fee. One attempt per
   game. Reported net per $1 staked, fill rate, fortnightly slices, and the
   holdout Brier of a logistic recalibration vs the market.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from research.calibration import brier, fit_logistic, logit, sigmoid
from research.kalshi_history import CACHE_DIR, _f, _get, _ts

logger = logging.getLogger(__name__)

SERIES = {"KXMLBGAME": 0.5, "KXNBAGAME": 1.0, "KXNHLGAME": 1.0}   # fee multipliers (Sept 2026)
WINDOW_S = 30 * 60           # tape kept before close
ENTRY_WINDOW_S = 10 * 60     # frozen: final 10 minutes
MIN_LEADER = 0.60            # frozen
TICK = 0.01
MAKER_RATE = 0.0175
TAKER_RATE = 0.07
MAX_PAGES = 8


def settled_games(series: str, days: int) -> dict[str, dict]:
    """{event_ticker: market dict of the first team market} for settled games."""
    out: dict[str, dict] = {}
    cur = ""
    since = int(time.time()) - days * 86400
    while True:
        d = _get(f"/markets?series_ticker={series}&status=settled&limit=200&min_close_ts={since}"
                 + (f"&cursor={cur}" if cur else ""))
        for m in d.get("markets", []):
            if m.get("result") not in ("yes", "no"):
                continue
            ev = m["event_ticker"]
            if ev not in out:
                out[ev] = {"ticker": m["ticker"], "event": ev, "result": m["result"],
                           "close_ts": _ts(m["close_time"]), "open_ts": _ts(m["open_time"]),
                           "volume": _f(m.get("volume_fp"), 0.0), "series": series}
        cur = d.get("cursor") or ""
        if not cur:
            break
    return out


def tape(ticker: str, close_ts: int, window_s: int = WINDOW_S) -> list[tuple]:
    """[(ts, yes_price, count)] ascending, from close-window to close."""
    out = []
    cur = ""
    for _ in range(MAX_PAGES):
        d = _get(f"/markets/trades?ticker={ticker}&limit=1000&min_ts={close_ts - window_s}&max_ts={close_ts + 120}"
                 + (f"&cursor={cur}" if cur else ""))
        trs = d.get("trades", [])
        for t in trs:
            ts = _ts(t.get("created_time"))
            px = _f(t.get("yes_price_dollars"))
            if ts and px is not None:
                # taker_side "yes" = taker BOUGHT yes (lifted the ask);
                # "no" = taker bought no = SOLD yes (hit the bid).
                out.append((ts, px, _f(t.get("count_fp"), 0.0), str(t.get("taker_side") or "")))
        cur = d.get("cursor") or ""
        if not cur or not trs:
            break
    out.sort()
    return out


def build(days: int, out_path: Path, series=tuple(SERIES), window_s: int = WINDOW_S,
          with_anchors: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    if with_anchors:
        from research.mlb_state import find_game, inning_anchors
    with out_path.open("w") as fh:
        for s in series:
            games = settled_games(s, days)
            logger.info("%s: %d settled games", s, len(games))
            for ev, g in games.items():
                if not g["close_ts"]:
                    continue
                anchors = None
                if with_anchors and s == "KXMLBGAME":
                    try:
                        gm = find_game(ev)
                        anchors = inning_anchors(gm["gamePk"]) if gm else None
                    except Exception as e:  # noqa: BLE001
                        logger.warning("anchors failed %s: %s", ev, e)
                    if not anchors or not anchors.get("top9_start"):
                        continue
                try:
                    tp = tape(g["ticker"], g["close_ts"], window_s=window_s)
                except Exception as e:  # noqa: BLE001
                    logger.warning("tape failed %s: %s", g["ticker"], e)
                    continue
                if len(tp) < 5:
                    continue
                row = {**g, "y": 1 if g["result"] == "yes" else 0,
                       "tape": [(t - g["close_ts"], p, c, side) for t, p, c, side in tp]}
                if anchors:
                    row["anchors"] = {k: (v - g["close_ts"] if isinstance(v, int) else v) for k, v in anchors.items()}
                fh.write(json.dumps(row) + "\n")
                n += 1
                if n % 50 == 0:
                    logger.info("k1 dataset: %d games", n)
    logger.info("written %s (%d games)", out_path, n)
    return out_path


# ---- evaluation ---------------------------------------------------------------------

def leader_obs(tape_rel: list, seconds_before: int):
    """Last print at or before close - seconds_before -> (leader_side, leader_price, yes_price)."""
    last = None
    for dt, p, *_ in tape_rel:
        if dt <= -seconds_before:
            last = p
        else:
            break
    if last is None:
        return None
    if last >= 0.5:
        return "YES", last, last
    return "NO", 1.0 - last, last


def reliability_by_minute(games: list[dict]) -> list[dict]:
    rows = []
    for mins in (30, 20, 10, 5, 2, 1):
        buckets: dict[str, list] = defaultdict(list)
        for g in games:
            o = leader_obs(g["tape"], mins * 60)
            if not o:
                continue
            side, lp, _ = o
            won = 1 if ((side == "YES") == (g["y"] == 1)) else 0
            b = "0.50-0.70" if lp < 0.70 else "0.70-0.85" if lp < 0.85 else "0.85-0.95" if lp < 0.95 else "0.95-1.00"
            buckets[b].append((lp, won))
        for b, xs in sorted(buckets.items()):
            rows.append({"minutes_to_close": mins, "bucket": b, "n": len(xs),
                         "mean_price": round(sum(p for p, _ in xs) / len(xs), 4),
                         "hit_rate": round(sum(w for _, w in xs) / len(xs), 4)})
    return rows


def strategy(games: list[dict], fee_mult: dict, maker: bool = True, strict: bool = True) -> dict:
    """Frozen K1 rule, one attempt per game.

    Fill model (conservative): our resting bid on the leader fills only when a
    LATER print shows an aggressor selling the leader (taker on the other
    side) at a price at or below our bid — `strict` requires strictly below,
    i.e. the seller went THROUGH our level, so queue position cannot matter."""
    results = []
    for g in games:
        tp = g["tape"]
        # first print inside the entry window with the leader >= MIN_LEADER
        entry = None
        for i, (dt, p, *_) in enumerate(tp):
            if dt < -ENTRY_WINDOW_S:
                continue
            side = "YES" if p >= 0.5 else "NO"
            lp = p if side == "YES" else 1.0 - p
            if lp >= MIN_LEADER:
                entry = (i, side, lp)
                break
        if entry is None:
            results.append({"event": g["event"], "close_ts": g["close_ts"], "attempted": False})
            continue
        i, side, lp = entry
        bid = round(lp - TICK, 4)
        # conservative fill: a later print at or below our bid in the leader's own leg
        filled_at = None
        for dt, p, _c, tside in tp[i + 1:]:
            own = p if side == "YES" else 1.0 - p
            # seller of the leader: taker bought the OTHER outcome
            seller = (tside == "no") if side == "YES" else (tside == "yes")
            if not seller:
                continue
            if (own < bid - 1e-9) if strict else (own <= bid + 1e-9):
                filled_at = dt
                break
        won = 1 if ((side == "YES") == (g["y"] == 1)) else 0
        m = fee_mult.get(g["series"], 1.0)
        fee = (MAKER_RATE if maker else TAKER_RATE) * m * bid * (1 - bid)
        pnl = (won - bid - fee) if filled_at is not None else 0.0
        results.append({"event": g["event"], "close_ts": g["close_ts"], "attempted": True,
                        "side": side, "bid": bid, "filled": filled_at is not None,
                        "won": won, "pnl_per_contract": round(pnl, 4), "stake": bid if filled_at is not None else 0.0})
    att = [r for r in results if r["attempted"]]
    fills = [r for r in att if r["filled"]]
    stake = sum(r["stake"] for r in fills)
    net = sum(r["pnl_per_contract"] for r in fills)
    # fortnightly slices
    slices = defaultdict(lambda: {"n": 0, "stake": 0.0, "net": 0.0})
    for r in fills:
        k = datetime.fromtimestamp(r["close_ts"], tz=timezone.utc).strftime("%Y-%m") + ("a" if datetime.fromtimestamp(r["close_ts"], tz=timezone.utc).day <= 15 else "b")
        slices[k]["n"] += 1
        slices[k]["stake"] += r["stake"]
        slices[k]["net"] += r["pnl_per_contract"]
    slice_rows = [{"slice": k, "n": v["n"], "net_per_dollar": round(v["net"] / v["stake"], 4) if v["stake"] else None}
                  for k, v in sorted(slices.items())]
    return {"games": len(results), "attempted": len(att), "filled": len(fills),
            "fill_rate": round(len(fills) / len(att), 3) if att else None,
            "win_rate": round(sum(r["won"] for r in fills) / len(fills), 3) if fills else None,
            "net_per_dollar": round(net / stake, 4) if stake else None,
            "net_per_dollar_fills_haircut_50": round(net / stake, 4) if stake else None,   # linear in fills: unchanged per $
            "avg_bid": round(sum(r["bid"] for r in fills) / len(fills), 4) if fills else None,
            "slices": slice_rows}


def strategy_anchor(games: list[dict], fee_mult: dict, anchor: str = "top9_start") -> dict:
    """K1 with a LIVE-IMPLEMENTABLE trigger: at the start of the 9th inning
    (MLB Stats API), if the leader prints >= MIN_LEADER, rest a bid one tick
    below the last print; strict seller-through fill; hold to settlement."""
    results = []
    for g in games:
        t0 = (g.get("anchors") or {}).get(anchor)
        tp = g["tape"]
        if t0 is None or not tp or t0 < tp[0][0] or t0 > 0:
            continue
        last = None
        idx = None
        for i, (dt, p, *_) in enumerate(tp):
            if dt <= t0:
                last, idx = p, i
            else:
                break
        if last is None:
            continue
        side = "YES" if last >= 0.5 else "NO"
        lp = last if side == "YES" else 1.0 - last
        if lp < MIN_LEADER:
            results.append({"attempted": False, "close_ts": g["close_ts"]})
            continue
        bid = round(lp - TICK, 4)
        filled = False
        for dt, p, _c, tside in tp[idx + 1:]:
            own = p if side == "YES" else 1.0 - p
            seller = (tside == "no") if side == "YES" else (tside == "yes")
            if seller and own < bid - 1e-9:
                filled = True
                break
        won = 1 if ((side == "YES") == (g["y"] == 1)) else 0
        fee = MAKER_RATE * fee_mult.get(g["series"], 1.0) * bid * (1 - bid)
        results.append({"attempted": True, "close_ts": g["close_ts"], "bid": bid, "filled": filled,
                        "won": won, "pnl": (won - bid - fee) if filled else 0.0, "stake": bid if filled else 0.0})
    att = [r for r in results if r["attempted"]]
    fills = [r for r in att if r["filled"]]
    stake = sum(r["stake"] for r in fills)
    net = sum(r["pnl"] for r in fills)
    slices = defaultdict(lambda: {"n": 0, "stake": 0.0, "net": 0.0})
    for r in fills:
        d = datetime.fromtimestamp(r["close_ts"], tz=timezone.utc)
        k = d.strftime("%Y-%m") + ("a" if d.day <= 15 else "b")
        slices[k]["n"] += 1
        slices[k]["stake"] += r["stake"]
        slices[k]["net"] += r["pnl"]
    return {"anchor": anchor, "games_with_anchor": len(results), "attempted": len(att), "filled": len(fills),
            "fill_rate": round(len(fills) / len(att), 3) if att else None,
            "win_rate": round(sum(r["won"] for r in fills) / len(fills), 3) if fills else None,
            "net_per_dollar": round(net / stake, 4) if stake else None,
            "avg_bid": round(sum(r["bid"] for r in fills) / len(fills), 4) if fills else None,
            "slices": [{"slice": k, "n": v["n"], "net_per_dollar": round(v["net"] / v["stake"], 4) if v["stake"] else None}
                       for k, v in sorted(slices.items())]}


def calibration_holdout(games: list[dict]) -> dict:
    """Logistic recalibration of the leader's 10-minute price; chronological 70/30."""
    obs = []
    for g in games:
        o = leader_obs(g["tape"], ENTRY_WINDOW_S)
        if not o:
            continue
        _, lp, yp = o
        obs.append((g["close_ts"], yp, g["y"]))
    obs.sort()
    k = int(len(obs) * 0.7)
    tr, te = obs[:k], obs[k:]
    if len(te) < 20:
        return {"n": len(obs)}
    a, b = fit_logistic([logit(p) for _, p, _ in tr], [y for _, _, y in tr])
    bm = brier([p for _, p, _ in te], [y for _, _, y in te])
    bc = brier([sigmoid(a + b * logit(p)) for _, p, _ in te], [y for _, _, y in te])
    return {"n": len(obs), "n_test": len(te), "a": round(a, 4), "b": round(b, 4),
            "brier_market": round(bm, 5), "brier_calibrated": round(bc, 5), "improves": bc < bm}


def _net_significant(sm: dict) -> bool:
    n, w, b, net = sm.get("filled") or 0, sm.get("win_rate"), sm.get("avg_bid"), sm.get("net_per_dollar")
    if not n or w is None or not b or net is None:
        return False
    sd = math.sqrt(max(w * (1 - w), 1e-6)) / b
    return net > 2 * sd / math.sqrt(n)


def evaluate(path: Path) -> dict:
    games = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    games.sort(key=lambda g: g["close_ts"])
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "games": len(games),
           "by_series": {s: sum(1 for g in games if g["series"] == s) for s in SERIES},
           "reliability": reliability_by_minute(games),
           "strategy_maker": strategy(games, SERIES, maker=True, strict=True),
           "strategy_maker_lenient_fills": strategy(games, SERIES, maker=True, strict=False),
           "strategy_taker_reference": strategy(games, SERIES, maker=False, strict=True),
           "calibration": calibration_holdout(games)}
    if any(g.get("anchors") for g in games):
        rep["strategy_inning9"] = strategy_anchor(games, SERIES, "top9_start")
        rep["strategy_bottom9_robustness"] = strategy_anchor(games, SERIES, "bot9_start")
    # The verdict is decided on the LIVE-IMPLEMENTABLE anchor when present.
    # "N minutes before close" is only known ex post and conditions on the
    # game ending soon (i.e. on the leader closing it out) — a lookahead.
    sm = rep.get("strategy_inning9") or rep["strategy_maker"]
    rep["verdict_basis"] = "strategy_inning9" if rep.get("strategy_inning9") else "strategy_maker (ex-post close anchor; NOT implementable)"
    gates = {
        "slices_positive_3_of_4": sum(1 for s in sm["slices"] if (s["net_per_dollar"] or 0) > 0) >= min(3, len(sm["slices"])) and len(sm["slices"]) >= 3,
        "brier_improves": bool(rep["calibration"].get("improves")),
        "fill_rate_ge_25pct": (sm["fill_rate"] or 0) >= 0.25,
        "net_positive": (sm["net_per_dollar"] or 0) > 0,
        # t-stat on per-fill return: mean / (sd/sqrt(n)) with sd ~ sqrt(w(1-w))/bid
        "net_gt_2se": _net_significant(sm),
    }
    rep["gates"] = gates
    rep["verdict"] = "PASS" if all(gates.values()) else "FAIL"
    return rep


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--data", default=str(CACHE_DIR / "k1_games.jsonl"))
    ap.add_argument("--out", default="docs/research/k1_report.json")
    ap.add_argument("--anchors", action="store_true", help="MLB Stats API inning anchors + 75-min tape")
    ap.add_argument("--window-min", type=int, default=30)
    a = ap.parse_args()
    p = Path(a.data)
    if a.build or not p.exists():
        build(a.days, p, window_s=a.window_min * 60, with_anchors=a.anchors)
    rep = evaluate(p)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=1))
    keys = [k for k in ("games", "by_series", "strategy_maker", "strategy_inning9", "strategy_bottom9_robustness",
                        "calibration", "gates", "verdict") if k in rep]
    print(json.dumps({k: rep[k] for k in keys}, indent=1))
    for r in rep["reliability"]:
        print(f"{r['minutes_to_close']:>3}m {r['bucket']}  n={r['n']:>4}  price {r['mean_price']:.3f}  hit {r['hit_rate']:.3f}  gap {r['hit_rate']-r['mean_price']:+.3f}")
