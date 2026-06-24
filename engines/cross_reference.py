"""
Cross-venue reference enrichment -- single entry point for main.py.

Keeps the main loop change to one call. Fetches Polymarket reference prices
(read-only) and attaches a confident cross_platform_price to matching Kalshi
markets, which re-enables the scanner's divergence signal. Fully fail-safe:
any error returns 0 and leaves markets untouched (bot scans Kalshi-only).

Toggle with env CROSS_VENUE_REFERENCE=0 to disable entirely.
"""
import os
import logging
from connectors.polymarket import PolymarketReference
from engines.market_matcher import attach_cross_prices

_log = logging.getLogger("predbot.xref")
_ref = None

def _enabled() -> bool:
    return os.getenv("CROSS_VENUE_REFERENCE", "1").strip().lower() not in ("0", "false", "no", "")

def enrich_with_cross_reference(markets: list, config=None, logger=None) -> int:
    """Attach Polymarket reference prices to `markets` in place.
    Returns number of Polymarket reference markets fetched (for dashboard).
    Never raises."""
    global _ref
    log = logger or _log
    if not _enabled():
        return 0
    try:
        if _ref is None:
            _ref = PolymarketReference({"enabled": True})
        poly = _ref.fetch_reference_markets()
        if not poly:
            return 0
        matched = attach_cross_prices(markets, poly, logger=log)
        log.info(
            f"Cross-venue: {len(poly)} Polymarket refs, {matched} confident "
            f"match(es) attached to Kalshi markets"
        )
        return len(poly)
    except Exception as e:
        log.warning(f"Cross-venue reference disabled this cycle: {type(e).__name__}: {e}")
        return 0
