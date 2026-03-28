"""
Polymarket CLOB API Connector
Uses py-clob-client SDK for authenticated operations,
falls back to raw HTTP for public endpoints.
"""

import json
import time
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PolymarketConnector:
    """Unified interface to Polymarket's APIs."""

    def __init__(self, config: dict):
        self.private_key = config.get("private_key", "")
        self.chain_id = config.get("chain_id", 137)
        self.funder = config.get("funder_address", "")
        self.client = None  # py-clob-client ClobClient (lazy init)
        self._rate_limit_remaining = 100
        self._last_request = 0
        self._api_reachable = None  # None=untested, True/False=cached

    def _init_client(self):
        """Lazy-initialize the authenticated CLOB client."""
        if self.client:
            return True
        if not self.private_key:
            logger.warning("No Polymarket private key -- running in read-only mode")
            return False
        try:
            from py_clob_client.client import ClobClient
            self.client = ClobClient(
                CLOB_API,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=1,
                funder=self.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info("Polymarket CLOB client initialized (authenticated)")
            return True
        except ImportError:
            logger.warning("py-clob-client not installed -- read-only mode")
            return False
        except Exception as e:
            logger.error(f"Failed to init Polymarket client: {e}")
            return False

    def _throttle(self):
        """Simple rate limiter: 60 req/min for trading, 100 for public."""
        elapsed = time.time() - self._last_request
        if elapsed < 0.7:  # ~85 req/min max
            time.sleep(0.7 - elapsed)
        self._last_request = time.time()

    def _http_get(self, url: str, timeout: int = 3) -> Optional[dict]:
        """Raw GET request for public endpoints. Caches reachability."""
        if self._api_reachable is False:
            return None  # Skip all calls if API confirmed unreachable
        self._throttle()
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Polymarket API unreachable -- skipping all calls: {e}")
            else:
                logger.debug(f"Polymarket HTTP failed: {url[:60]} -- {e}")
            return None

    # ── Market Data (public, no auth) ──

    def get_markets(self, limit=100, active=True) -> list:
        """Fetch active markets from Gamma API."""
        url = f"{GAMMA_API}/markets?limit={limit}&active={'true' if active else 'false'}&closed=false"
        data = self._http_get(url)
        return data if isinstance(data, list) else []

    def get_market(self, condition_id: str) -> Optional[dict]:
        """Get details for a single market."""
        url = f"{GAMMA_API}/markets?condition_id={condition_id}"
        data = self._http_get(url)
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Get market by its slug/URL identifier."""
        url = f"{GAMMA_API}/markets?slug={slug}"
        data = self._http_get(url)
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Get full orderbook for a token (public)."""
        url = f"{CLOB_API}/book?token_id={token_id}"
        return self._http_get(url)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        url = f"{CLOB_API}/midpoint?token_id={token_id}"
        data = self._http_get(url)
        if data and "mid" in data:
            try:
                return float(data["mid"])
            except (ValueError, TypeError):
                return None
        return None

    def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Get best price for a side (buy/sell)."""
        url = f"{CLOB_API}/price?token_id={token_id}&side={side}"
        data = self._http_get(url)
        if data and "price" in data:
            try:
                return float(data["price"])
            except (ValueError, TypeError):
                return None
        return None

    def get_spread(self, token_id: str) -> Optional[dict]:
        """Get spread info (bid, ask, mid, spread)."""
        url = f"{CLOB_API}/spread?token_id={token_id}"
        return self._http_get(url)

    # ── Trading (requires auth) ──

    def place_order(self, token_id: str, side: str, price: float, size: float) -> Optional[dict]:
        """Place a limit order. Returns order dict or None."""
        if not self._init_client():
            logger.error("Cannot place order -- client not authenticated")
            return None
        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            order_side = BUY if side.lower() == "buy" else SELL
            order = self.client.create_order({
                "token_id": token_id,
                "price": price,
                "size": size,
                "side": order_side,
            })
            result = self.client.post_order(order)
            logger.info(f"Order placed: {side} {size} @ {price} -- {result}")
            return result
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._init_client():
            return False
        try:
            self.client.cancel(order_id)
            logger.info(f"Order canceled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def get_positions(self) -> list:
        """Get current positions (requires auth)."""
        if not self._init_client():
            return []
        try:
            return self.client.get_positions() or []
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            return []

    # ── Utility ──

    def scan_markets_with_prices(self, limit=50) -> list:
        """
        Fetch markets with enriched price data.
        Uses outcomePrices from Gamma API (already in /markets response)
        to avoid N+1 midpoint calls. Only fetches live midpoint for
        top markets by volume.
        """
        markets = self.get_markets(limit=limit)
        if not markets:
            logger.warning("Polymarket returned 0 markets")
            return []

        enriched = []
        for m in markets:
            try:
                tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
                if not tokens:
                    continue

                # Use outcomePrices from Gamma response (no extra API call)
                yes_price = None
                no_price = None
                outcome_prices = m.get("outcomePrices")
                if outcome_prices:
                    try:
                        if isinstance(outcome_prices, str):
                            prices = json.loads(outcome_prices)
                        else:
                            prices = outcome_prices
                        if prices and len(prices) >= 1:
                            yes_price = float(prices[0])
                        if prices and len(prices) >= 2:
                            no_price = float(prices[1])
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass

                # Fallback: bestBid/bestAsk fields
                if yes_price is None:
                    best_bid = m.get("bestBid")
                    best_ask = m.get("bestAsk")
                    if best_bid and best_ask:
                        try:
                            yes_price = (float(best_bid) + float(best_ask)) / 2
                        except (ValueError, TypeError):
                            pass

                if yes_price is None:
                    continue  # No price data

                if no_price is None:
                    no_price = 1.0 - yes_price

                enriched.append({
                    "platform": "polymarket",
                    "question": m.get("question", ""),
                    "condition_id": m.get("conditionId", ""),
                    "market_id": m.get("conditionId", ""),  # Alias for cross-engine compat
                    "slug": m.get("slug", ""),
                    "token_ids": tokens,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": float(m.get("volume", 0) or 0),
                    "volume_24h": float(m.get("volume24hr", 0) or 0),
                    "liquidity": float(m.get("liquidity", 0) or 0),
                    "end_date": m.get("endDate", ""),
                    "raw": m,
                })
            except Exception as e:
                logger.debug(f"Skipping market {m.get('question','?')}: {e}")
                continue

        logger.info(f"Polymarket: enriched {len(enriched)} markets (batch prices, no round-trips)")
        return enriched

    def is_connected(self) -> bool:
        """Test API connectivity."""
        data = self._http_get(f"{CLOB_API}/time")
        return data is not None
