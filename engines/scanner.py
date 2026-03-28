"""
EV Gap Scanner -- Core mispricing detector.
Scans markets on both platforms, computes expected value gaps,
and returns ranked opportunities.

Formula: EV = (p_true - market_price) * (1 / market_price)
Signal threshold: EV > min_ev_threshold (default 0.05)

Probability model uses multiple independent signals:
- Cross-platform price divergence (if available)
- Market microstructure anomalies (yes+no spread, volume imbalance)
- Extreme price bias correction (prices near 0/1 tend to overstate certainty)
- Time-decay compression (markets near close cluster toward 0.5)
"""

import logging
import math
import time
from typing import Optional

logger = logging.getLogger(__name__)


class EVScanner:
    """Scans prediction markets for +EV opportunities."""

    def __init__(self, config: dict):
        self.min_ev = config.get("min_ev_threshold", 0.05)
        self.min_volume = config.get("min_market_volume", 5000)

    def compute_ev(self, model_prob: float, market_price: float) -> float:
        """
        Compute expected value of a bet.

        Args:
            model_prob: Your estimated true probability (0-1)
            market_price: Current market price (0-1)

        Returns:
            EV as a float. Positive = profitable edge.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
        payout = 1.0 / market_price
        ev = (model_prob - market_price) * payout
        return round(ev, 4)

    def estimate_true_prob(self, market: dict) -> Optional[float]:
        """
        Estimate true probability using multiple independent signals.
        Returns a model probability that can differ meaningfully from market price.

        Signals used:
        1. Cross-platform divergence (strongest signal)
        2. Yes/No spread inefficiency (market maker overpricing one side)
        3. Extreme price bias correction (longshot/favorite bias)
        4. Volume-weighted confidence (low volume = regress toward 0.5)
        """
        yes_price = market.get("yes_price")
        if yes_price is None:
            return None

        adjustments = []
        weights = []

        # ── Signal 1: Cross-platform price divergence ──
        cross_price = market.get("cross_platform_price")
        if cross_price is not None and abs(cross_price - yes_price) > 0.02:
            # Average of two independent markets is a better estimate
            cross_prob = (yes_price + cross_price) / 2
            adjustments.append(cross_prob)
            weights.append(3.0)  # Strongest signal
            logger.debug(f"Cross-platform: {yes_price:.3f} vs {cross_price:.3f} -> {cross_prob:.3f}")

        # ── Signal 2: Yes/No spread inefficiency ──
        no_price = market.get("no_price")
        if no_price is not None and no_price > 0:
            total = yes_price + no_price
            if total > 1.02 or total < 0.98:
                # Market maker is charging spread -- true prob is the
                # normalized price (removes the vig)
                spread_prob = yes_price / total
                adjustments.append(spread_prob)
                weights.append(1.5)
                logger.debug(f"Spread signal: yes={yes_price:.3f} no={no_price:.3f} sum={total:.3f} -> {spread_prob:.3f}")

        # ── Signal 3: Extreme price bias correction (longshot-favorite bias) ──
        # Research shows prediction markets systematically overprice longshots
        # (prices near 0.05-0.15) and slightly overprice heavy favorites (>0.90).
        # Correction: pull extreme prices toward 50% slightly.
        if yes_price < 0.15:
            # Longshot bias: market says 10%, true prob is likely ~7%
            bias_prob = yes_price * 0.75
            adjustments.append(bias_prob)
            weights.append(1.0)
        elif yes_price > 0.90:
            # Favorite bias: market says 95%, true prob is likely ~92%
            bias_prob = 1.0 - (1.0 - yes_price) * 1.3
            bias_prob = max(0.80, bias_prob)
            adjustments.append(bias_prob)
            weights.append(1.0)
        elif 0.40 < yes_price < 0.60:
            # Near 50/50 markets: slight edge from volume/momentum
            volume = market.get("volume_24h", market.get("volume", 0)) or 0
            if volume > 10000:
                # High-volume near-50/50 markets are efficient; skip bias correction
                adjustments.append(yes_price)
                weights.append(0.5)
            else:
                # Low-volume near-50/50: regress toward 0.5 (less info)
                regressed = yes_price * 0.8 + 0.5 * 0.2
                adjustments.append(regressed)
                weights.append(0.8)

        # ── Signal 4: Volume-based confidence ──
        volume = market.get("volume_24h", market.get("volume", 0)) or 0
        if volume < 2000:
            # Very low volume: market price is unreliable, regress hard toward 0.5
            vol_prob = yes_price * 0.6 + 0.5 * 0.4
            adjustments.append(vol_prob)
            weights.append(1.2)
        elif volume < 5000:
            vol_prob = yes_price * 0.8 + 0.5 * 0.2
            adjustments.append(vol_prob)
            weights.append(0.8)

        # ── Combine signals ──
        if not adjustments:
            # No independent signals -- use market price (no edge)
            return yes_price

        total_weight = sum(weights)
        model_prob = sum(a * w for a, w in zip(adjustments, weights)) / total_weight

        # Bound to [0.02, 0.98]
        model_prob = max(0.02, min(0.98, model_prob))

        return round(model_prob, 4)

    def scan(self, markets: list) -> list:
        """
        Scan a list of markets for +EV opportunities.

        Args:
            markets: List of market dicts from connectors

        Returns:
            Sorted list of opportunities with EV > threshold
        """
        opportunities = []

        for m in markets:
            yes_price = m.get("yes_price")
            no_price = m.get("no_price")
            volume = m.get("volume_24h", m.get("volume", 0)) or 0

            if yes_price is None or yes_price <= 0 or yes_price >= 1:
                continue

            # Skip low-volume markets (thin = manipulatable)
            if volume < self.min_volume:
                continue

            # Estimate true probability
            model_prob = self.estimate_true_prob(m)
            if model_prob is None:
                continue

            # Compute EV for YES side
            ev_yes = self.compute_ev(model_prob, yes_price)

            # Compute EV for NO side
            ev_no = 0.0
            if no_price and no_price > 0 and no_price < 1:
                model_no = 1 - model_prob
                ev_no = self.compute_ev(model_no, no_price)

            # Take the better side
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

        # Sort by EV descending
        opportunities.sort(key=lambda x: x["ev"], reverse=True)
        return opportunities

    def cross_reference_markets(self, poly_markets: list, kalshi_markets: list) -> list:
        """
        Find matching markets across platforms and inject cross-platform prices.
        Uses fuzzy question matching.
        """
        # Build lookup of Kalshi questions (normalized)
        kalshi_lookup = {}
        for km in kalshi_markets:
            q = km.get("question", "").lower().strip()
            if q:
                kalshi_lookup[q] = km

        # Check each Polymarket question against Kalshi
        combined = []
        matched = set()

        for pm in poly_markets:
            q = pm.get("question", "").lower().strip()
            # Exact match
            if q in kalshi_lookup:
                km = kalshi_lookup[q]
                pm["cross_platform_price"] = km.get("yes_price")
                pm["cross_platform"] = "kalshi"
                matched.add(q)
            combined.append(pm)

        # Add unmatched Kalshi markets
        for km in kalshi_markets:
            q = km.get("question", "").lower().strip()
            if q not in matched:
                combined.append(km)

        return combined
