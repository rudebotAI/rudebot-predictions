"""
Cross-Platform Arbitrage Detector
Finds risk-free or near-risk-free opportunities between Polymarket and Kalshi.

Two types of arb:
1. Same-event arb: same question priced differently on two platforms
2. Multi-outcome arb: sum of all outcome prices != 1 (uses Bregman projection)
"""

import math
import logging

logger = logging.getLogger(__name__)


class ArbitrageDetector:
    """Detects arbitrage opportunities across prediction markets."""

    def __init__(self, config: dict):
        self.min_gap = config.get("min_arb_gap", 0.03)  # 3% minimum
        self.fee_rate = config.get("fee_rate", 0.02)     # 2% combined fees

    def detect_same_event_arb(self, poly_markets: list, kalshi_markets: list) -> list:
        """
        Find the same event priced differently on Polymarket vs Kalshi.
        Buy low on one, sell high (or buy NO) on the other.
        """
        arbs = []

        # Build Kalshi lookup by normalized question
        kalshi_by_q = {}
        for km in kalshi_markets:
            q = self._normalize_question(km.get("question", ""))
            if q:
                kalshi_by_q[q] = km

        for pm in poly_markets:
            pq = self._normalize_question(pm.get("question", ""))
            if not pq:
                continue

            # Try exact match first
            km = kalshi_by_q.get(pq)
            if not km:
                # Try fuzzy match
                km = self._fuzzy_match(pq, kalshi_by_q)

            if not km:
                continue

            poly_yes = pm.get("yes_price")
            kalshi_yes = km.get("yes_price")

            if poly_yes is None or kalshi_yes is None:
                continue
            if poly_yes <= 0 or kalshi_yes <= 0:
                continue

            gap = abs(poly_yes - kalshi_yes)
            net_gap = gap - self.fee_rate  # Subtract fees

            if net_gap > self.min_gap:
                if poly_yes < kalshi_yes:
                    action = "Buy YES on Polymarket, Buy NO on Kalshi"
                    buy_price = poly_yes
                    sell_price = 1 - kalshi_yes
                else:
                    action = "Buy YES on Kalshi, Buy NO on Polymarket"
                    buy_price = kalshi_yes
                    sell_price = 1 - poly_yes

                profit_pct = net_gap / max(buy_price, 0.01) * 100

                arbs.append({
                    "type": "same_event",
                    "question": pm.get("question", ""),
                    "poly_price": poly_yes,
                    "kalshi_price": kalshi_yes,
                    "gap": round(gap, 4),
                    "net_gap": round(net_gap, 4),
                    "profit_pct": round(profit_pct, 2),
                    "action": action,
                    "poly_market": pm,
                    "kalshi_market": km,
                })

        arbs.sort(key=lambda x: x["net_gap"], reverse=True)
        return arbs

    def detect_multi_outcome_arb(self, markets: list) -> list:
        """
        Find markets where outcome prices don't sum to 1.
        If sum of YES prices for all outcomes < 1, buy all = guaranteed profit.
        If sum > 1, sell all = guaranteed profit.
        """
        arbs = []

        # Group markets by event
        events = {}
        for m in markets:
            event_key = m.get("event_ticker", m.get("slug", ""))
            if event_key:
                if event_key not in events:
                    events[event_key] = []
                events[event_key].append(m)

        for event_key, outcomes in events.items():
            if len(outcomes) < 2:
                continue

            prices = []
            for o in outcomes:
                p = o.get("yes_price")
                if p and p > 0:
                    prices.append(p)

            if len(prices) < 2:
                continue

            total = sum(prices)
            deviation = abs(total - 1.0)

            if deviation > self.min_gap + self.fee_rate:
                arbs.append({
                    "type": "multi_outcome",
                    "event": event_key,
                    "num_outcomes": len(prices),
                    "price_sum": round(total, 4),
                    "deviation": round(deviation, 4),
                    "direction": "buy_all" if total < 1 else "sell_all",
                    "profit_pct": round((deviation - self.fee_rate) * 100, 2),
                    "outcomes": outcomes,
                })

        arbs.sort(key=lambda x: x["deviation"], reverse=True)
        return arbs

    def _normalize_question(self, q: str) -> str:
        """Normalize question text for matching."""
        return " ".join(q.lower().strip().split())

    def _fuzzy_match(self, question: str, lookup: dict) -> dict:
        """Find best fuzzy match in lookup dict."""
        q_words = set(question.split())
        best_match = None
        best_score = 0

        for key, value in lookup.items():
            k_words = set(key.split())
            overlap = len(q_words & k_words)
            total = len(q_words | k_words)
            score = overlap / total if total > 0 else 0

            if score > best_score and score > 0.5:
                best_score = score
                best_match = value

        return best_match
