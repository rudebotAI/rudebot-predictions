"""
Fair Value Model -- Institutional-Grade Edge Calculation
========================================================
Computes fair value for prediction market contracts using multiple
independent pricing models, then calculates edge in basis points.

Models:
1. Cross-platform consensus (strongest: different market makers)
2. Orderbook-implied fair value (microprice, depth-weighted)
3. Technical momentum overlay (for crypto markets)
4. Vig-stripped probability (remove market maker spread)
5. Bayesian posterior from research signals

Edge = (fair_value - market_price) in basis points (1 bp = 0.01%)
Only trade when edge > min_edge_bps (default: 200 bps = 2%)

Reference: txbabaxyz/mlmodelpoly fair_value_model.py
"""

import math
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FairValueModel:
    """
    Multi-model fair value estimator with edge calculation.

    Usage:
        fv = FairValueModel(config)
        result = fv.compute(market, ws_feed=feed, crypto_analysis=analysis)
        if result["edge_bps"] > 200:
            # Trade!
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.min_edge_bps = config.get("min_edge_bps", 200)  # 2% minimum edge
        self.max_edge_bps = config.get("max_edge_bps", 5000)  # 50% max (likely data error)
        self.weights = config.get("model_weights", {
            "cross_platform": 3.0,
            "orderbook": 2.5,
            "vig_stripped": 1.5,
            "momentum": 1.0,
            "volume_regression": 0.8,
        })

    def compute(
        self,
        market: dict,
        ws_feed=None,
        crypto_analysis: dict = None,
        research_lr: float = 1.0,
    ) -> dict:
        """
        Compute fair value and edge for a market.

        Args:
            market: Market dict from connector
            ws_feed: Optional WebSocketFeed for real-time data
            crypto_analysis: Optional CryptoMomentumEngine analysis
            research_lr: Bayesian likelihood ratio from research layer

        Returns:
            Dict with fair_value, edge_bps, model_details, tradeable
        """
        yes_price = market.get("yes_price")
        if yes_price is None or yes_price <= 0 or yes_price >= 1:
            return {"fair_value": None, "edge_bps": 0, "tradeable": False, "reason": "invalid_price"}

        estimates = []
        model_details = {}

        # ── Model 1: Cross-Platform Consensus ──
        cross_price = market.get("cross_platform_price")
        if cross_price is not None and abs(cross_price - yes_price) > 0.01:
            # Inverse-variance weighted average (more weight to the deeper market)
            cross_vol = market.get("cross_platform_volume", 10000)
            own_vol = market.get("volume_24h", market.get("volume", 0)) or 1000
            w_cross = math.sqrt(cross_vol)
            w_own = math.sqrt(own_vol)
            consensus = (cross_price * w_cross + yes_price * w_own) / (w_cross + w_own)
            estimates.append(("cross_platform", consensus, self.weights["cross_platform"]))
            model_details["cross_platform"] = {
                "estimate": round(consensus, 4),
                "divergence": round(abs(cross_price - yes_price), 4),
            }

        # ── Model 2: Orderbook-Implied Fair Value ──
        if ws_feed:
            token_ids = market.get("token_ids", [])
            if token_ids:
                microprice = ws_feed.get_microprice(token_ids[0])
                if microprice is not None:
                    estimates.append(("orderbook", microprice, self.weights["orderbook"]))
                    model_details["orderbook"] = {
                        "microprice": round(microprice, 6),
                        "spread": ws_feed.get_spread(token_ids[0]),
                        "imbalance": ws_feed.get_imbalance(token_ids[0]),
                    }

        # ── Model 3: Vig-Stripped Probability ──
        no_price = market.get("no_price")
        if no_price is not None and no_price > 0:
            total = yes_price + no_price
            if total > 1.001:  # Overround exists (vig)
                vig_stripped = yes_price / total
                vig = total - 1.0
                estimates.append(("vig_stripped", vig_stripped, self.weights["vig_stripped"]))
                model_details["vig_stripped"] = {
                    "estimate": round(vig_stripped, 4),
                    "vig": round(vig, 4),
                    "overround": round(total, 4),
                }

        # ── Model 4: Momentum Overlay (crypto only) ──
        if crypto_analysis:
            direction_prob = crypto_analysis.get("directional_prob", 0.5)
            confidence = crypto_analysis.get("confidence", 0)

            if confidence > 0.1:  # Only use if somewhat confident
                # Blend momentum signal with market price
                momentum_fv = yes_price * 0.6 + direction_prob * 0.4
                estimates.append(("momentum", momentum_fv, self.weights["momentum"] * confidence))
                model_details["momentum"] = {
                    "estimate": round(momentum_fv, 4),
                    "direction_prob": round(direction_prob, 4),
                    "confidence": round(confidence, 4),
                    "signal": crypto_analysis.get("signal", "NEUTRAL"),
                }

        # ── Model 5: Volume-Based Regression ──
        volume = market.get("volume_24h", market.get("volume", 0)) or 0
        if volume < 5000:
            # Low volume -> regress toward 0.5 (less price discovery)
            regression_strength = max(0, 1.0 - volume / 5000) * 0.3
            vol_fv = yes_price * (1 - regression_strength) + 0.5 * regression_strength
            estimates.append(("volume_regression", vol_fv, self.weights["volume_regression"]))
            model_details["volume_regression"] = {
                "estimate": round(vol_fv, 4),
                "volume": volume,
                "regression_strength": round(regression_strength, 4),
            }

        # ── Combine Models ──
        if not estimates:
            return {
                "fair_value": yes_price,
                "edge_bps": 0,
                "tradeable": False,
                "reason": "no_models_available",
                "models": model_details,
            }

        total_weight = sum(w for _, _, w in estimates)
        fair_value = sum(est * w for _, est, w in estimates) / total_weight

        # Apply Bayesian research adjustment
        if research_lr != 1.0 and abs(research_lr - 1.0) > 0.05:
            # Convert to odds, apply LR, convert back
            if 0.01 < fair_value < 0.99:
                odds = fair_value / (1 - fair_value)
                odds *= research_lr
                fair_value = odds / (1 + odds)
                model_details["research_adjustment"] = {
                    "lr": round(research_lr, 3),
                    "adjusted_fv": round(fair_value, 4),
                }

        # Bound to [0.02, 0.98]
        fair_value = max(0.02, min(0.98, fair_value))

        # ── Compute Edge in Basis Points ──
        edge_raw = fair_value - yes_price
        edge_bps = int(edge_raw * 10000)  # Convert to basis points

        # Determine tradeable side
        if edge_bps > 0:
            side = "YES"
        elif edge_bps < 0:
            # Check NO side
            if no_price and no_price > 0:
                no_edge = (1 - fair_value) - no_price
                no_edge_bps = int(no_edge * 10000)
                if no_edge_bps > abs(edge_bps):
                    edge_bps = no_edge_bps
                    side = "NO"
                else:
                    side = "YES"
            else:
                side = "YES"
        else:
            side = "NONE"

        abs_edge = abs(edge_bps)
        tradeable = self.min_edge_bps <= abs_edge <= self.max_edge_bps

        result = {
            "fair_value": round(fair_value, 4),
            "market_price": yes_price,
            "edge_bps": edge_bps,
            "edge_pct": round(edge_raw * 100, 2),
            "side": side,
            "tradeable": tradeable,
            "models_used": len(estimates),
            "model_names": [name for name, _, _ in estimates],
            "models": model_details,
            "reason": "tradeable" if tradeable else (
                "edge_too_small" if abs_edge < self.min_edge_bps else "edge_too_large"
            ),
        }

        if tradeable:
            logger.info(
                f"[FV] {market.get('question', '')[:40]} | "
                f"FV={fair_value:.4f} vs Market={yes_price:.4f} | "
                f"Edge={edge_bps}bps ({side}) | "
                f"Models: {', '.join(result['model_names'])}"
            )

        return result

    def compute_ev_from_fv(self, fair_value: float, market_price: float) -> float:
        """Compute EV using fair value as the probability estimate."""
        if market_price <= 0 or market_price >= 1:
            return 0.0
        payout = 1.0 / market_price
        return round((fair_value - market_price) * payout, 4)
