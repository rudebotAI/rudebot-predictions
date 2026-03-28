"""
KL-Divergence Cross-Market Scanner
Measures "distance" between probability distributions of correlated markets.

Formula: D_KL(P||Q) = Σ P_i * log(P_i / Q_i)
High KL (>0.2) between correlated markets = arb opportunity.
"""

import math
import logging

logger = logging.getLogger(__name__)


class DivergenceScanner:
    """Finds correlated markets with divergent pricing."""

    def __init__(self, kl_threshold: float = 0.2):
        self.kl_threshold = kl_threshold

    def kl_divergence(self, p: list, q: list) -> float:
        """
        Compute KL-divergence D_KL(P||Q).

        Args:
            p: Probability distribution P (list of floats summing to ~1)
            q: Probability distribution Q (list of floats summing to ~1)

        Returns:
            KL-divergence (non-negative float, 0 = identical)
        """
        if len(p) != len(q):
            return 0.0

        kl = 0.0
        for pi, qi in zip(p, q):
            # Smooth to avoid log(0)
            pi = max(pi, 1e-10)
            qi = max(qi, 1e-10)
            kl += pi * math.log(pi / qi)

        return max(0.0, kl)

    def symmetric_kl(self, p: list, q: list) -> float:
        """Symmetric KL: (D_KL(P||Q) + D_KL(Q||P)) / 2."""
        return (self.kl_divergence(p, q) + self.kl_divergence(q, p)) / 2

    def find_divergences(self, markets: list) -> list:
        """
        Find pairs of correlated markets with divergent pricing.

        Compares all pairs and returns those exceeding KL threshold.
        Useful for: same question on different platforms, or related outcomes.
        """
        divergences = []

        for i in range(len(markets)):
            for j in range(i + 1, len(markets)):
                m1 = markets[i]
                m2 = markets[j]

                p1_yes = m1.get("yes_price")
                p2_yes = m2.get("yes_price")
                if p1_yes is None or p2_yes is None:
                    continue
                if p1_yes <= 0 or p1_yes >= 1 or p2_yes <= 0 or p2_yes >= 1:
                    continue

                # Binary distributions
                dist1 = [p1_yes, 1 - p1_yes]
                dist2 = [p2_yes, 1 - p2_yes]

                kl = self.symmetric_kl(dist1, dist2)

                if kl > self.kl_threshold:
                    divergences.append({
                        "market_1": m1.get("question", "?"),
                        "market_2": m2.get("question", "?"),
                        "platform_1": m1.get("platform", "?"),
                        "platform_2": m2.get("platform", "?"),
                        "price_1": p1_yes,
                        "price_2": p2_yes,
                        "kl_divergence": round(kl, 4),
                        "price_gap": round(abs(p1_yes - p2_yes), 4),
                        "direction": "buy_1_sell_2" if p1_yes < p2_yes else "buy_2_sell_1",
                    })

        divergences.sort(key=lambda x: x["kl_divergence"], reverse=True)
        return divergences

    def scan_cross_platform(self, poly_markets: list, kalshi_markets: list) -> list:
        """
        Specifically scan for the same question priced differently across platforms.
        This is the highest-confidence arb signal.
        """
        # Simple fuzzy matching on question text
        matches = []

        for pm in poly_markets:
            pq = pm.get("question", "").lower().strip()
            if not pq:
                continue

            for km in kalshi_markets:
                kq = km.get("question", "").lower().strip()
                if not kq:
                    continue

                # Check for significant word overlap
                p_words = set(pq.split())
                k_words = set(kq.split())
                overlap = len(p_words & k_words)
                total = len(p_words | k_words)

                if total > 0 and overlap / total > 0.5:  # >50% word overlap
                    p_price = pm.get("yes_price")
                    k_price = km.get("yes_price")
                    if p_price and k_price:
                        dist1 = [p_price, 1 - p_price]
                        dist2 = [k_price, 1 - k_price]
                        kl = self.symmetric_kl(dist1, dist2)

                        if abs(p_price - k_price) > 0.03:  # >3% gap
                            matches.append({
                                "poly_question": pm.get("question"),
                                "kalshi_question": km.get("question"),
                                "poly_price": p_price,
                                "kalshi_price": k_price,
                                "kl_divergence": round(kl, 4),
                                "gap": round(abs(p_price - k_price), 4),
                                "word_overlap": round(overlap / total, 2),
                            })

        matches.sort(key=lambda x: x["gap"], reverse=True)
        return matches
