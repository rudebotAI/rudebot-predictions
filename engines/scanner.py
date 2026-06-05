"""
EV Gap Scanner -- Core mispricing detector.
Scans markets on Kalshi, computes expected value gaps,
and returns ranked opportunities.

Formula: EV = (p_true - market_price) / market_price
Signal threshold: EV > min_ev_threshold (default 0.05)

Probability model uses multiple independent signals:
- Cross-event price divergence (when same outcome priced in multiple markets)
- Market microstructure anomalies (yes+no spread, volume imbalance)
- Extreme price bias correction (prices near 0/1 tend to overstate certainty)
- Volume-weighted confidence (low volume = regress toward 0.5)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Hard ceiling on returned EV. Values above this are almost always
# stale/wrong upstream data (e.g. price feed lag on a fast-moving market,
# or a model_prob mismatch with reality). We clamp silently rather than
# skipping so a single bad data point doesn't kill an otherwise-good scan
# cycle; the sanity gate in main.py will still flag if many opportunities
# come back at the cap, which would indicate a real upstream bug.
EV_CLAMP_MAX = 5.0

# Kalshi trading fee: 0.07 * price * (1 - price) dollars per contract.
# Edges smaller than the fee are structurally net-negative -- ignoring
# this systematically overrates longshots and coin-flip markets.
KALSHI_FEE_RATE = 0.07


def parse_days_to_resolution(end_date: str) -> Optional[float]:
    """
    Days from now until the market's close/resolution time.
    Accepts ISO-8601 (Kalshi close_time, e.g. '2026-09-01T15:00:00Z').
    Returns None if missing/unparseable.
    """
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


class EVScanner:
    """Scans prediction markets for +EV opportunities."""

    def __init__(self, config: dict):
        self.min_ev = config.get("min_ev_threshold", 0.05)
        self.min_volume = config.get("min_market_volume", 5000)
        # Markets resolving further out than this are skipped entirely:
        # a +EV position that pays out in 2045 is dead capital, and edge
        # estimates degrade with horizon anyway.
        self.max_days_to_resolution = config.get("max_days_to_resolution", 90)
        self.fee_rate = config.get("fee_rate", KALSHI_FEE_RATE)

    def compute_ev(self, model_prob: float, market_price: float) -> float:
        """
        Fee-adjusted expected value per dollar staked.

        Per contract: cost = market_price, payout = 1 if win, and the
        exchange charges fee_rate * price * (1 - price) on entry. EV per
        dollar = (p_true - price - fee) / price. Edges below the fee floor
        come back as 0 (no opportunity).

        Returns:
            EV as a float, clamped to [0, EV_CLAMP_MAX]. Negative edges
            return 0 (no opportunity), preserving the original behavior
            where only positive-EV opportunities matter.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
        fee = self.fee_rate * market_price * (1.0 - market_price)
        ev = (model_prob - market_price - fee) / market_price
        if ev < 0:
            return 0.0
        if ev > EV_CLAMP_MAX:
            logger.debug(
                "compute_ev: clamping EV %.3f to %.3f (market_price=%.4f model_prob=%.4f)",
                ev, EV_CLAMP_MAX, market_price, model_prob,
            )
            ev = EV_CLAMP_MAX
        return round(ev, 4)

    def estimate_true_prob(self, market: dict) -> Optional[float]:
        """
        Estimate true probability using multiple independent signals.

        Signals used:
        1. Cross-event price divergence (strongest signal)
        2. Yes/No spread inefficiency (market maker overpricing one side)
        3. Extreme price bias correction (longshot/favorite bias)
        4. Volume-weighted confidence (low volume = regress toward 0.5)
        """
        yes_price = market.get("yes_price")
        if yes_price is None:
            return None

        adjustments = []
        weights = []

        # ── Signal 1: Cross-event price divergence ──
        cross_price = market.get("cross_platform_price")
        if cross_price is not None and abs(cross_price - yes_price) > 0.02:
            cross_prob = (yes_price + cross_price) / 2
            adjustments.append(cross_prob)
            weights.append(3.0)
            logger.debug(
                f"Cross-event: {yes_price:.3f} vs {cross_price:.3f} -> {cross_prob:.3f}"
            )

        # ── Signal 2: Yes/No spread inefficiency ──
        no_price = market.get("no_price")
        if no_price is not None and no_price > 0:
            total = yes_price + no_price
            if total > 1.02 or total < 0.98:
                spread_prob = yes_price / total
                adjustments.append(spread_prob)
                weights.append(1.5)
                logger.debug(
                    f"Spread signal: yes={yes_price:.3f} no={no_price:.3f} sum={total:.3f} -> {spread_prob:.3f}"
                )

        # ── Signal 3: Extreme price bias correction ──
        if yes_price < 0.15:
            bias_prob = yes_price * 0.75
            adjustments.append(bias_prob)
            weights.append(1.0)
        elif yes_price > 0.90:
            bias_prob = 1.0 - (1.0 - yes_price) * 1.3
            bias_prob = max(0.80, bias_prob)
            adjustments.append(bias_prob)
            weights.append(1.0)
        elif 0.40 < yes_price < 0.60:
            volume = market.get("volume_24h", market.get("volume", 0)) or 0
            if volume > 10000:
                adjustments.append(yes_price)
                weights.append(0.5)
            else:
                regressed = yes_price * 0.8 + 0.5 * 0.2
                adjustments.append(regressed)
                weights.append(0.8)

        # ── Signal 4: Volume-based confidence ──
        volume = market.get("volume_24h", market.get("volume", 0)) or 0
        if volume < 2000:
            vol_prob = yes_price * 0.6 + 0.5 * 0.4
            adjustments.append(vol_prob)
            weights.append(1.2)
        elif volume < 5000:
            vol_prob = yes_price * 0.8 + 0.5 * 0.2
            adjustments.append(vol_prob)
            weights.append(0.8)

        # ── Combine signals ──
        if not adjustments:
            return yes_price

        total_weight = sum(weights)
        model_prob = sum(a * w for a, w in zip(adjustments, weights)) / total_weight
        model_prob = max(0.02, min(0.98, model_prob))
        return round(model_prob, 4)

    def scan(self, markets: list) -> list:
        """
        Scan a list of markets for +EV opportunities.

        Returns:
            Sorted list of opportunities with EV > threshold.
        """
        opportunities = []
        skipped_horizon = 0
        skipped_no_date = 0

        for m in markets:
            yes_price = m.get("yes_price")
            no_price = m.get("no_price")
            volume = m.get("volume_24h", m.get("volume", 0)) or 0

            if yes_price is None or yes_price <= 0 or yes_price >= 1:
                continue

            if volume < self.min_volume:
                continue

            # ── Resolution-horizon gate ──
            days = parse_days_to_resolution(m.get("end_date"))
            if days is None:
                skipped_no_date += 1
                continue
            if days <= 0 or days > self.max_days_to_resolution:
                skipped_horizon += 1
                continue

            model_prob = self.estimate_true_prob(m)
            if model_prob is None:
                continue

            ev_yes = self.compute_ev(model_prob, yes_price)
            ev_no = 0.0
            if no_price and no_price > 0 and no_price < 1:
                model_no = 1 - model_prob
                ev_no = self.compute_ev(model_no, no_price)

            # Annualized EV: EV per unit time. A 5% edge resolving next
            # week beats a 20% edge resolving next year. Floor at 1 day
            # to avoid same-day blowup (resolution_sniper owns that zone).
            ann_factor = 365.0 / max(days, 1.0)

            if ev_yes > ev_no and ev_yes > self.min_ev:
                opportunities.append({
                    **m,
                    "signal": "YES",
                    "ev": ev_yes,
                    "ev_annualized": round(ev_yes * ann_factor, 4),
                    "days_to_resolution": round(days, 1),
                    "model_prob": model_prob,
                    "market_price": yes_price,
                    "edge": round(model_prob - yes_price, 4),
                })
            elif ev_no > self.min_ev:
                opportunities.append({
                    **m,
                    "signal": "NO",
                    "ev": ev_no,
                    "ev_annualized": round(ev_no * ann_factor, 4),
                    "days_to_resolution": round(days, 1),
                    "model_prob": 1 - model_prob,
                    "market_price": no_price,
                    "edge": round((1 - model_prob) - no_price, 4),
                })

        if skipped_horizon or skipped_no_date:
            logger.info(
                f"Scanner horizon gate: skipped {skipped_horizon} markets resolving "
                f">{self.max_days_to_resolution}d out (or already closed), "
                f"{skipped_no_date} with missing/unparseable close time"
            )

        # Rank by capital efficiency (annualized EV), not raw EV.
        opportunities.sort(key=lambda x: x["ev_annualized"], reverse=True)
        return opportunities

    def cross_reference_markets(self, poly_markets: list, kalshi_markets: list) -> list:
        """
        Kept for back-compat (poly_markets is unused after Polymarket removal).
        Returns the kalshi_markets list unchanged. Future cross-event matching
        within Kalshi can land here.
        """
        return list(kalshi_markets)
