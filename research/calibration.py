"""
Calibration model — the Predict stage's evidence-based core.

Literature (Le 2026, 64.7M Kalshi trades; Whelan et al. 2025) shows Kalshi
prices are systematically mis-calibrated by CATEGORY and HORIZON: political
prices compressed toward 50% (underconfident), weather prices too extreme at
short horizons, sports near-calibrated mid-life. The exploitable object is
therefore not "our forecast" but the map

    P(outcome = yes | market price p, category c, horizon h)

which we estimate directly from Kalshi's own settled markets (free public
data, `research/kalshi_history.py`) as a logistic recalibration

    logit(P) = a_{c,h} + b_{c,h} * logit(p)

with b > 1 meaning the market is underconfident (push prices away from 50%)
and b < 1 overconfident (pull them in). Each (category, horizon) cell keeps
its n, a bootstrap CI on b, the market's Brier score vs the recalibrated
Brier score on a TIME-SPLIT holdout, and a reliability table. A cell is
only USED by the bot if the holdout Brier actually improves and the CI on
b excludes 1 — otherwise the cell falls back to the market price (no edge).

Pure Python (no numpy) so the runtime image stays small.
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path

HORIZON_KEYS = ("1", "6", "24", "72", "168")
MIN_CELL_N = 150            # below this the estimate is noise
MIN_MID_N = 50              # samples with 0.10 < p < 0.90 — a slope fitted on 1¢/99¢ prices is meaningless
MIN_EVENTS = 100            # independent events; strike ladders share one outcome and are NOT independent
SLOPE_BOUNDS = (0.5, 3.0)   # outside this the fit is separable junk, not a calibration
INTERCEPT_BOUND = 2.0
BOOTSTRAP = 200
MAX_ITERS = 200

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "calibration.json"


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1 / (1 + e)
    e = math.exp(z)
    return e / (1 + e)


# ---- 2-parameter logistic fit (Newton) ----------------------------------------------

def fit_logistic(xs: list[float], ys: list[int], l2: float = 1e-3) -> tuple[float, float]:
    """Fit logit(P) = a + b*x by Newton-Raphson with a tiny ridge for
    separability. Returns (a, b); (0, 1) is 'the market is calibrated'."""
    a, b = 0.0, 1.0
    n = len(xs)
    if n < 2:
        return a, b
    for _ in range(MAX_ITERS):
        g_a = g_b = 0.0
        h_aa = h_ab = h_bb = 0.0
        for x, y in zip(xs, ys):
            p = sigmoid(a + b * x)
            w = p * (1 - p)
            r = p - y
            g_a += r
            g_b += r * x
            h_aa += w
            h_ab += w * x
            h_bb += w * x * x
        g_a += l2 * a
        g_b += l2 * (b - 1.0)
        h_aa += l2
        h_bb += l2
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a -= da
        b -= db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    return a, b


def brier(ps: list[float], ys: list[int]) -> float:
    if not ps:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def reliability(ps: list[float], ys: list[int], edges=(0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0001)) -> list[dict]:
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi]
        if not sel:
            continue
        rows.append({"lo": lo, "hi": min(hi, 1.0), "n": len(sel),
                     "mean_price": round(sum(p for p, _ in sel) / len(sel), 4),
                     "hit_rate": round(sum(y for _, y in sel) / len(sel), 4)})
    return rows


# ---- per (category, horizon) cells -------------------------------------------------------

def _cells(rows: list[dict]) -> dict[tuple[str, str], list[tuple]]:
    """{(category, horizon): [(close_ts, price, y, event), ...]}. One row can
    feed several horizon cells; the horizon price must exist and be strictly
    inside (0, 1). `event` groups sibling markets (ladders share an outcome
    and a close) so splits and bootstraps never treat them as independent."""
    cells: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for i, r in enumerate(rows):
        c = r.get("category") or "?"
        ev = r.get("event_ticker") or r.get("ticker") or f"row{i}"
        for h, p in (r.get("p") or {}).items():
            if h in HORIZON_KEYS and p is not None and 0.0 < float(p) < 1.0:
                cells[(c, h)].append((int(r.get("close_ts") or 0), float(p), int(r["y"]), ev))
    return cells


def _split_by_event(samples: list[tuple], frac: float = 0.7) -> tuple[list, list]:
    """Chronological split on EVENT boundaries: every market of one event
    lands on the same side."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for s in sorted(samples, key=lambda t: (t[0], t[3])):
        if s[3] not in groups:
            groups[s[3]] = []
            order.append(s[3])
        groups[s[3]].append(s)
    n = len(samples)
    train, test, acc = [], [], 0
    for ev in order:
        (train if acc < n * frac else test).extend(groups[ev])
        acc += len(groups[ev])
    return train, test


def fit_cell(samples: list[tuple], seed: int = 0) -> dict:
    """Fit one cell with an event-grouped chronological 70/30 holdout and an
    event-bootstrap CI on the slope. `used` is the decision the bot honours."""
    samples = [tuple(s) if len(s) == 4 else (s[0], s[1], s[2], str(s[0])) for s in samples]
    n = len(samples)
    n_mid = sum(1 for _, p, _, _ in samples if 0.10 < p < 0.90)
    train, test = _split_by_event(samples)
    k = len(train)
    xs = [logit(p) for _, p, _, _ in train]
    ys = [y for _, _, y, _ in train]
    a, b = fit_logistic(xs, ys)
    # holdout, scored with the TRAIN fit
    tp = [p for _, p, _, _ in test]
    ty = [y for _, _, y, _ in test]
    brier_mkt = brier(tp, ty)
    brier_cal = brier([sigmoid(a + b * logit(p)) for p in tp], ty)
    # event bootstrap of the slope (train only)
    rng = random.Random(seed)
    groups: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(train):
        groups[s[3]].append(i)
    keys = list(groups)
    bs = []
    for _ in range(BOOTSTRAP):
        idx = [i for _ in range(len(keys)) for i in groups[keys[rng.randrange(len(keys))]]]
        bs.append(fit_logistic([xs[i] for i in idx], [ys[i] for i in idx])[1])
    bs.sort()
    lo, hi = (bs[int(0.025 * BOOTSTRAP)], bs[int(0.975 * BOOTSTRAP) - 1]) if bs else (b, b)
    # full-sample fit for deployment (after the holdout verdict)
    a_full, b_full = fit_logistic([logit(p) for _, p, _, _ in samples], [y for _, _, y, _ in samples])
    improves = (brier_cal < brier_mkt - 1e-6) if test else False
    slope_signif = (lo > 1.0) or (hi < 1.0)
    in_bounds = all(SLOPE_BOUNDS[0] <= v <= SLOPE_BOUNDS[1] for v in (b, b_full)) and \
        all(abs(v) <= INTERCEPT_BOUND for v in (a, a_full))
    n_events = len({s[3] for s in samples})
    why = ("ok" if (n >= MIN_CELL_N and n_events >= MIN_EVENTS and n_mid >= MIN_MID_N
                    and in_bounds and improves and slope_signif) else
           f"n={n}<{MIN_CELL_N}" if n < MIN_CELL_N else
           f"only {n_events} independent events (<{MIN_EVENTS})" if n_events < MIN_EVENTS else
           f"only {n_mid} mid-range prices (<{MIN_MID_N})" if n_mid < MIN_MID_N else
           f"fit out of bounds (a={a_full:.2f}, b={b_full:.2f})" if not in_bounds else
           "no holdout Brier improvement" if not improves else
           "slope CI includes 1")
    return {
        "n": n, "n_mid": n_mid, "n_train": k, "n_test": len(test),
        "n_events": n_events,
        "a": round(a_full, 4), "b": round(b_full, 4),
        "a_train": round(a, 4), "b_train": round(b, 4),
        "b_ci95": [round(lo, 3), round(hi, 3)],
        "brier_market_holdout": round(brier_mkt, 5) if test else None,
        "brier_calibrated_holdout": round(brier_cal, 5) if test else None,
        "brier_improvement": round(brier_mkt - brier_cal, 5) if test else None,
        "base_rate": round(sum(y for _, _, y, _ in samples) / n, 4),
        "reliability_market": reliability([p for _, p, _, _ in samples], [y for _, _, y, _ in samples]),
        "used": why == "ok",
        "why": why,
    }


def fit_all(rows: list[dict], seed: int = 0) -> dict:
    cells = _cells(rows)
    out: dict[str, dict] = {}
    for (c, h), samples in sorted(cells.items()):
        if len(samples) < 30:
            continue
        out[f"{c}|{h}"] = fit_cell(samples, seed=seed)
    # pooled (all categories) per horizon, as the fallback prior
    pooled: dict[str, list] = defaultdict(list)
    for (c, h), samples in cells.items():
        pooled[h].extend(samples)
    for h, samples in pooled.items():
        if len(samples) >= 30:
            out[f"ALL|{h}"] = fit_cell(samples, seed=seed)
    return out


def build_model(rows: list[dict], source: str, seed: int = 0) -> dict:
    from datetime import datetime, timezone
    cells = fit_all(rows, seed=seed)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source, "n_rows": len(rows),
        "horizons_h": list(HORIZON_KEYS),
        "min_cell_n": MIN_CELL_N,
        "cells": cells,
        "n_cells_used": sum(1 for k, c in cells.items() if c["used"] and not k.startswith("ALL|")),
    }


# ---- runtime side ------------------------------------------------------------------------

class CalibratedModel:
    """Maps (market price, category, hours to close) -> calibrated probability
    using the fitted cells. No usable cell for the market's own category and
    horizon means the market price itself (edge 0) — never a pooled slope. `explain()` tells the operator which cell fired."""

    def __init__(self, model: dict):
        self.model = model or {}
        self.cells: dict[str, dict] = (model or {}).get("cells", {})

    @classmethod
    def load(cls, path: Path | str = MODEL_PATH) -> "CalibratedModel":
        p = Path(path)
        if not p.exists():
            return cls({})
        return cls(json.loads(p.read_text()))

    @staticmethod
    def horizon_key(hours_to_close: float) -> str:
        # nearest fitted horizon on a log scale
        best, bd = HORIZON_KEYS[0], float("inf")
        for k in HORIZON_KEYS:
            d = abs(math.log(max(hours_to_close, 0.25)) - math.log(float(k)))
            if d < bd:
                best, bd = k, d
        return best

    def cell_for(self, category: str | None, hours_to_close: float) -> tuple[str, dict] | tuple[None, None]:
        """Only the market's OWN category cell is ever used. The pooled
        ALL|h cells are reported for context but never traded on: categories
        are mis-calibrated in OPPOSITE directions (politics underconfident,
        weather overconfident), so a pooled slope would be wrong for both."""
        h = self.horizon_key(hours_to_close)
        key = f"{category}|{h}"
        c = self.cells.get(key)
        if c and c.get("used"):
            return key, c
        return None, None

    def prob(self, price: float, category: str | None, hours_to_close: float) -> float:
        key, c = self.cell_for(category, hours_to_close)
        if not c:
            return price
        return sigmoid(c["a"] + c["b"] * logit(price))

    def explain(self, price: float, category: str | None, hours_to_close: float) -> dict:
        key, c = self.cell_for(category, hours_to_close)
        p = self.prob(price, category, hours_to_close)
        return {"cell": key, "model_prob": round(p, 4), "edge": round(p - price, 4),
                "slope": c["b"] if c else None, "n": c["n"] if c else 0,
                "reason": (f"calibration {key}: slope {c['b']:.2f}, n={c['n']}, "
                           f"holdout Brier {c['brier_market_holdout']}->{c['brier_calibrated_holdout']}")
                if c else "no usable calibration cell -> market price"}


if __name__ == "__main__":
    import argparse
    import logging
    from research.kalshi_history import load_dataset
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--out", default=str(MODEL_PATH))
    a = ap.parse_args()
    rows = load_dataset(Path(a.dataset))
    model = build_model(rows, source=a.dataset)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(model, indent=1))
    print(f"{len(rows)} rows -> {len(model['cells'])} cells, {model['n_cells_used']} usable -> {a.out}")
    for k, c in model["cells"].items():
        print(f"{k:<32} n={c['n']:>5} b={c['b']:>6.2f} ci={c['b_ci95']} "
              f"brier mkt {c['brier_market_holdout']} cal {c['brier_calibrated_holdout']} used={c['used']} ({c['why']})")
