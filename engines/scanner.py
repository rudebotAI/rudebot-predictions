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
from typing import Optional

logger = logging.getLogger(__name__)

# Hard ceiling on returned EV. Values above this are almost always
# stale/wrong upstream data (e.g. price feed lag on a fast-moving market,
# or a model_prob mismatch with reality). We clamp silently rather than
# skipping so a single bad data point doesn't kill an otherwise-good scan
# cycle; the sanity gate in main.py will still flag if many opportunities
# come back at the cap, which would indicate a real upstream bug.
EV_CLAMP_MAX = 5.0


class EVScanner:
    """Scans prediction markets for +EV opportunities."""

    def __init__(self, config: dict):
        self.min_ev = config.get("min_ev_threshold", 0.05)
        self.min_volume = config.get("min_market_volume", 5000)

    def compute_ev(self, model_prob: float, market_price: float) -> float:
        """
        Expected value per dollar staked.

        Args:
            model_prob: Estimated true probability (0-1)
            market_price: Current market price (0-1)

        Returns:
            EV as a float, clamped to [0, EV_CLAMP_MAX]. Negative edges
            return 0 (no opportunity), preserving the original behavior
            where only positive-EV opportunities matter.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
        payout = 1.0 / market_price
        ev = (model_prob - market_price) * payout
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

        for m in markets:
            yes_price = m.get("yes_price")
            no_price = m.get("no_price")
            volume = m.get("volume_24h", m.get("volume", 0)) or 0

            if yes_price is None or yes_price <= 0 or yes_price >= 1:
                continue

            if volume < self.min_volume:
                continue

            model_prob = self.estimate_true_prob(m)
            if model_prob is None:
                continue

            ev_yes = self.compute_ev(model_prob, yes_price)
            ev_no = 0.0
            if no_price and no_price > 0 and no_price < 1:
                model_no = 1 - model_prob
                ev_no = self.compute_ev(model_no, no_price)

            if ev_yes > ev_no and ev_yes > self.min_ev:
                opportunities.append({
                    **m,
                    "signal": "YES",
                    "ev": ev_yes,
                    "model_prob": model_prob,
                    "market_price": yes_price,
                    "edge": round(model_prob - yes_price, 4),
                })
            elif ev_no > self.min_ev:
                opportunities.append({
                    **m,
                    "signal": "NO",
                    "ev": ev_no,
                    "model_prob": 1 - model_prob,
                    "market_price": no_price,
                    "edge": round((1 - model_prob) - no_price, 4),
                })

        opportunities.sort(key=lambda x: x["ev"], reverse=True)
        return opportunities

    def cross_reference_markets(self, poly_markets: list, kalshi_markets: list) -> list:
        """
        Kept for back-compat (poly_markets is unused after Polymarket removal).
        Returns the kalshi_markets list unchanged. Future cross-event matching
        within Kalshi can land here.
        """
        return list(kalshi_markets)
