"""
Kelly Criterion Position Sizing Engine
Computes optimal bet sizes for long-term compounding.

Formula: f* = (p * odds - (1-p)) / odds
Uses fractional Kelly (default 0.25x) for safety.
"""

import logging

logger = logging.getLogger(__name__)


class KellySizer:
    """Computes optimal position sizes using Kelly Criterion."""

    def __init__(self, config: dict):
        self.fraction = config.get("kelly_fraction", 0.25)
        self.max_position_usd = config.get("max_position_usd", 10.0)
        self.max_portfolio_pct = config.get("max_portfolio_pct", 0.10)

    def kelly_fraction_calc(self, prob: float, market_price: float) -> float:
        """
        Compute Kelly fraction for a binary bet.

        Args:
            prob: Your estimated true probability of winning
            market_price: Current market price (your cost per share, 0-1)

        Returns:
            Optimal fraction of bankroll to bet (before applying safety fraction)
        """
        if market_price <= 0 or market_price >= 1 or prob <= 0 or prob >= 1:
            return 0.0

        # Odds = payout ratio - 1 = (1/price) - 1
        odds = (1.0 / market_price) - 1.0
        if odds <= 0:
            return 0.0

        # Kelly formula: f* = (p * odds - (1-p)) / odds
        f_star = (prob * odds - (1 - prob)) / odds

        # Never recommend negative sizing (would mean bet the other side)
        return max(0.0, f_star)

    def compute_size(self, prob: float, market_price: float, bankroll: float) -> dict:
        """
        Compute recommended position size in USD.

        Args:
            prob: Your model's probability estimate
            market_price: Current market price
            bankroll: Current total bankroll in USD

        Returns:
            Dict with sizing details: kelly_raw, kelly_frac, size_usd, shares
        """
        kelly_raw = self.kelly_fraction_calc(prob, market_price)
        kelly_frac = kelly_raw * self.fraction  # Apply safety fraction

        # Compute USD size
        size_usd = bankroll * kelly_frac

        # Apply caps
        size_usd = min(size_usd, self.max_position_usd)
        size_usd = min(size_usd, bankroll * self.max_portfolio_pct)
        size_usd = max(0, round(size_usd, 2))

        # Number of shares at current price
        shares = size_usd / market_price if market_price > 0 else 0

        result = {
            "kelly_raw": round(kelly_raw, 4),
            "kelly_fractional": round(kelly_frac, 4),
            "fraction_used": self.fraction,
            "size_usd": size_usd,
            "shares": round(shares, 2),
            "bankroll": bankroll,
            "prob": prob,
            "market_price": market_price,
        }

        if kelly_raw > 0:
            logger.info(
                f"Kelly: raw={kelly_raw:.3f} frac={kelly_frac:.3f} -> ${size_usd:.2f} "
                f"({shares:.1f} shares @ {market_price:.3f})"
            )
        else:
            logger.debug(f"Kelly: no edge (raw={kelly_raw:.3f})")

        return result

    def should_bet(self, prob: float, market_price: float) -> bool:
        """Quick check: does Kelly say to bet at all?"""
        return self.kelly_fraction_calc(prob, market_price) > 0.001
