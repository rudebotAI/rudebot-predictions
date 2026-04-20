"""
Limitless Venue Adapter (U8)

Third prediction-market venue adapter. Mirrors the interface of the
existing polymarket and kalshi connectors so the scanner + arbitrage
engines can reason about Limitless markets without venue-specific logic.

Note: Limitless is on-chain (Base). This adapter reads public on-chain
data and the Limitless HTTP API for market metadata. Writes (order
placement) are deliberately NOT implemented here — enabling live trading
on Limitless requires additional wallet + approval setup that should be
handled as a separate hardening pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "api_base": "https://api.limitless.exchange",
    "chain_rpc": "https://mainnet.base.org",
    "timeout_sec": 10,
}


@dataclass
class LimitlessMarket:
    market_id: str
    title: str
    yes_price: float
    no_price: float
    volume_usd: float
    depth_usd: float
    resolves_at_epoch: Optional[float]


class LimitlessConnector:
    VENUE = "limitless"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = {**DEFAULTS, **(cfg.get("limitless") or {})}

    # ----- read side ------------------------------------------------------
    def list_markets(self, limit: int = 50) -> List[LimitlessMarket]:
        if not self.cfg.get("enabled", False):
            return []
        try:
            import requests
        except ImportError:
            log.error("limitless connector requires 'requests'")
            return []

        url = f"{self.cfg['api_base']}/markets?limit={int(limit)}&status=active"
        try:
            r = requests.get(url, timeout=self.cfg["timeout_sec"])
            r.raise_for_status()
            data = r.json() or []
        except Exception as e:  # noqa: BLE001
            log.warning("limitless list_markets failed: %s", e)
            return []

        out: List[LimitlessMarket] = []
        for m in data:
            try:
                out.append(LimitlessMarket(
                    market_id=str(m.get("id")),
                    title=str(m.get("title", "")),
                    yes_price=float(m.get("yesPrice", 0.0)),
                    no_price=float(m.get("noPrice", 0.0)),
                    volume_usd=float(m.get("volumeUsd", 0.0)),
                    depth_usd=float(m.get("depthUsd", 0.0)),
                    resolves_at_epoch=m.get("resolvesAt"),
                ))
            except (TypeError, ValueError):
                continue
        return out

    def get_orderbook(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Return normalized book: {bids: [[price, size], ...], asks: [[...]]}."""
        if not self.cfg.get("enabled", False):
            return None
        try:
            import requests
        except ImportError:
            return None
        url = f"{self.cfg['api_base']}/markets/{market_id}/orderbook"
        try:
            r = requests.get(url, timeout=self.cfg["timeout_sec"])
            r.raise_for_status()
            j = r.json() or {}
        except Exception as e:  # noqa: BLE001
            log.warning("limitless get_orderbook failed: %s", e)
            return None
        return {
            "market_id": market_id,
            "bids": j.get("bids") or [],
            "asks": j.get("asks") or [],
        }

    # ----- write side ------------------------------------------------------
    def place_order(self, *args, **kwargs):
        raise NotImplementedError(
            "limitless.place_order is intentionally not implemented. "
            "Live execution on Limitless requires a separate signing + "
            "approval workflow. Enable paper mode only for this venue."
        )
