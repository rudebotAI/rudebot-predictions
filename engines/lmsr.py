"""
LMSR (Logarithmic Market Scoring Rule) Price Impact Engine
Models how trades move Polymarket prices.

Formula: Price_i = e^(q_i / b) / Σ e^(q_j / b)
Where q = quantity vector, b = liquidity depth parameter.

Small b = prices move more per trade = bigger edge opportunities.
"""

import math
import logging

logger = logging.getLogger(__name__)


class LMSREngine:
    """Computes LMSR price impact and identifies thin-pool opportunities."""

    def price(self, q_yes: float, q_no: float, b: float) -> tuple:
        """
        Compute LMSR prices for a binary market.

        Args:
            q_yes: Outstanding YES shares
            q_no: Outstanding NO shares
            b: Liquidity parameter (higher = deeper pool)

        Returns:
            (yes_price, no_price) tuple
        """
        if b <= 0:
            return (0.5, 0.5)
        exp_yes = math.exp(q_yes / b)
        exp_no = math.exp(q_no / b)
        total = exp_yes + exp_no
        return (exp_yes / total, exp_no / total)

    def cost_to_buy(self, q_yes: float, q_no: float, b: float, shares: float, side: str = "yes") -> float:
        """
        Compute the cost (in USD) to buy `shares` of a side.

        This is the integral of the price curve from current state to new state.
        Cost = b * ln(e^(q_yes+shares)/b + e^(q_no/b)) - b * ln(e^(q_yes/b) + e^(q_no/b))
        """
        if b <= 0:
            return shares * 0.5

        def cost_func(qy, qn):
            return b * math.log(math.exp(qy / b) + math.exp(qn / b))

        before = cost_func(q_yes, q_no)
        if side == "yes":
            after = cost_func(q_yes + shares, q_no)
        else:
            after = cost_func(q_yes, q_no + shares)

        return after - before

    def price_impact(self, current_price: float, shares: float, b: float) -> float:
        """
        Estimate how much buying `shares` will move the price.

        Args:
            current_price: Current YES price (0-1)
            shares: Number of shares to buy
            b: Liquidity parameter

        Returns:
            New price after the purchase
        """
        if b <= 0 or current_price <= 0 or current_price >= 1:
            return current_price

        # Reverse-engineer q_yes from current price (assuming q_no=0)
        # price = e^(q/b) / (e^(q/b) + 1) -> q = b * ln(price / (1-price))
        q_yes = b * math.log(current_price / (1 - current_price))
        q_no = 0

        new_yes, _ = self.price(q_yes + shares, q_no, b)
        return new_yes

    def estimate_b(self, market: dict) -> float:
        """
        Estimate the LMSR liquidity parameter b from market data.

        Heuristic: b ~ liquidity / 2 for Polymarket-style markets.
        Lower b = thinner pool = more exploitable.
        """
        liquidity = market.get("liquidity", 0) or 0
        volume = market.get("volume_24h", market.get("volume", 0)) or 0

        if liquidity > 0:
            return max(10, liquidity / 2)
        elif volume > 0:
            return max(10, volume / 20)
        return 100  # Default assumption

    def is_thin_pool(self, b: float, threshold: float = 50) -> bool:
        """Check if a pool is thin enough to be manipulatable (or exploitable)."""
        return b < threshold

    def analyze_market(self, market: dict, trade_size_usd: float = 10) -> dict:
        """
        Full LMSR analysis for a market.

        Returns impact metrics for your intended trade size.
        """
        b = self.estimate_b(market)
        yes_price = market.get("yes_price", 0.5) or 0.5

        # How many shares does $trade_size buy at current price?
        shares = trade_size_usd / yes_price if yes_price > 0 else 0

        new_price = self.price_impact(yes_price, shares, b)
        impact_pct = abs(new_price - yes_price) / yes_price * 100 if yes_price > 0 else 0

        return {
            "b_estimate": round(b, 1),
            "is_thin": self.is_thin_pool(b),
            "current_price": yes_price,
            "shares_for_trade": round(shares, 2),
            "price_after_trade": round(new_price, 4),
            "impact_pct": round(impact_pct, 2),
            "slippage_usd": round(abs(new_price - yes_price) * shares, 4),
        }
