"""
Bayesian Probability Updater
Adjusts probability estimates based on new evidence.

Formula: P(H|E) = P(E|H) * P(H) / P(E)
Each piece of evidence nudges the estimate toward truth.
"""

import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BayesianUpdater:
    """Updates probability beliefs with incoming evidence."""

    def __init__(self):
        self.beliefs = {}  # market_id -> BayesianBelief

    def update(self, prior: float, likelihood_ratio: float) -> float:
        """
        Single Bayesian update.

        Args:
            prior: Current probability estimate P(H) (0-1)
            likelihood_ratio: P(E|H) / P(E|~H)
                > 1 means evidence supports H
                < 1 means evidence opposes H
                = 1 means evidence is uninformative

        Returns:
            Posterior probability P(H|E)
        """
        if prior <= 0 or prior >= 1 or likelihood_ratio <= 0:
            return prior

        # Convert to odds, multiply by LR, convert back
        prior_odds = prior / (1 - prior)
        posterior_odds = prior_odds * likelihood_ratio
        posterior = posterior_odds / (1 + posterior_odds)

        return max(0.001, min(0.999, posterior))

    def multi_update(self, prior: float, evidence_list: list) -> float:
        """
        Apply multiple pieces of evidence sequentially.

        Args:
            prior: Starting probability
            evidence_list: List of dicts with 'likelihood_ratio' and optional 'weight'

        Returns:
            Final posterior after all updates
        """
        current = prior
        for ev in evidence_list:
            lr = ev.get("likelihood_ratio", 1.0)
            weight = ev.get("weight", 1.0)
            # Weight < 1 dampens the evidence strength
            adjusted_lr = 1.0 + (lr - 1.0) * weight
            current = self.update(current, adjusted_lr)
        return current

    def track_market(self, market_id: str, initial_price: float):
        """Start tracking a market for Bayesian updates."""
        self.beliefs[market_id] = {
            "current_prob": initial_price,
            "initial_price": initial_price,
            "updates": [],
            "created": datetime.now(timezone.utc).isoformat(),
        }

    def add_evidence(self, market_id: str, evidence_type: str,
                     likelihood_ratio: float, description: str = ""):
        """
        Add evidence to a tracked market.

        evidence_type: "volume_spike", "price_movement", "news", "cross_platform", "sentiment"
        """
        if market_id not in self.beliefs:
            return

        belief = self.beliefs[market_id]
        old_prob = belief["current_prob"]
        new_prob = self.update(old_prob, likelihood_ratio)

        belief["current_prob"] = new_prob
        belief["updates"].append({
            "type": evidence_type,
            "lr": likelihood_ratio,
            "old_prob": round(old_prob, 4),
            "new_prob": round(new_prob, 4),
            "description": description,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            f"Bayesian update [{market_id}]: {old_prob:.3f} -> {new_prob:.3f} "
            f"(LR={likelihood_ratio:.2f}, type={evidence_type})"
        )

    def get_belief(self, market_id: str) -> dict:
        """Get current belief state for a market."""
        return self.beliefs.get(market_id, {})

    def get_edge(self, market_id: str, market_price: float) -> float:
        """Get edge: our belief minus market price."""
        belief = self.beliefs.get(market_id)
        if not belief:
            return 0.0
        return belief["current_prob"] - market_price

    # ── Evidence generators (heuristics) ──

    @staticmethod
    def volume_spike_lr(current_volume: float, avg_volume: float) -> float:
        """
        Likelihood ratio from a volume spike.
        Higher volume = more informed trading = prices more likely correct.
        """
        if avg_volume <= 0:
            return 1.0
        ratio = current_volume / avg_volume
        if ratio > 3.0:
            return 1.3  # Strong signal toward current price
        elif ratio > 1.5:
            return 1.1
        return 1.0

    @staticmethod
    def price_momentum_lr(price_change_pct: float) -> float:
        """
        Likelihood ratio from recent price movement.
        Strong momentum = prices likely to continue.
        """
        if price_change_pct > 10:
            return 1.25
        elif price_change_pct > 5:
            return 1.1
        elif price_change_pct < -10:
            return 0.8
        elif price_change_pct < -5:
            return 0.9
        return 1.0

    @staticmethod
    def cross_platform_lr(our_price: float, other_price: float) -> float:
        """
        Likelihood ratio from cross-platform price difference.
        If another platform prices higher, evidence toward YES.
        """
        diff = other_price - our_price
        if abs(diff) < 0.02:
            return 1.0
        # Scale: 10% diff -> LR of 1.5
        return 1.0 + diff * 5
