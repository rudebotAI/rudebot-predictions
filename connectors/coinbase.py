"""
Coinbase Price Feed -- Real-time spot prices and candles.
Matches the diagram: Coinbase API -> price -> MCP plugin -> Strategy engine.

Uses Coinbase Advanced Trade API (v3) for candles with Binance fallback.
Spot prices via Coinbase v2 public API.
"""

import json
import time
import logging
import urllib.request
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

COINBASE_API = "https://api.coinbase.com/v2"
COINBASE_ADVANCED = "https://api.coinbase.com/api/v3/brokerage"

# Granularity mapping: seconds -> Coinbase Advanced Trade API granularity strings
_GRANULARITY_MAP = {
    60: "ONE_MINUTE",
    300: "FIVE_MINUTE",
    900: "FIFTEEN_MINUTE",
    1800: "THIRTY_MINUTE",
    3600: "ONE_HOUR",
    7200: "TWO_HOUR",
    21600: "SIX_HOUR",
    86400: "ONE_DAY",
}

# Binance interval mapping (fallback)
_BINANCE_INTERVAL_MAP = {
    60: "1m", 300: "5m", 900: "15m", 1800: "30m",
    3600: "1h", 7200: "2h", 21600: "6h", 86400: "1d",
}

# Map product IDs between exchanges
_BINANCE_SYMBOL_MAP = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
    "MATIC-USD": "MATICUSDT",
}


class CoinbaseConnector:
    """Real-time price feed from Coinbase + Binance fallback."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.cache = {}
        self.cache_ttl = config.get("price_cache_ttl", 30)  # 30s cache
        self._last_request = 0
        self._api_reachable = None

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request = time.time()

    def _http_get(self, url: str, headers: dict = None, timeout: int = 3) -> Optional[dict]:
        if self._api_reachable is False:
            return None
        self._throttle()
        try:
            hdrs = {"Accept": "application/json", "User-Agent": "PredBot/4.0"}
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Coinbase/Binance API unreachable -- skipping: {e}")
            else:
                logger.debug(f"HTTP GET failed: {url[:80]} -- {e}")
            return None

    def get_spot_price(self, pair: str = "BTC-USD") -> Optional[float]:
        """
        Get current spot price. Tries Coinbase v2, then Binance fallback.
        Cached for cache_ttl seconds.
        """
        cache_key = f"spot_{pair}"
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached["ts"] < self.cache_ttl:
                return cached["price"]

        price = self._coinbase_spot(pair) or self._binance_spot(pair)
        if price is not None:
            self.cache[cache_key] = {"price": price, "ts": time.time()}
        return price

    def _coinbase_spot(self, pair: str) -> Optional[float]:
        """Coinbase v2 spot price."""
        data = self._http_get(f"{COINBASE_API}/prices/{pair}/spot")
        if data and "data" in data:
            try:
                return float(data["data"]["amount"])
            except (ValueError, KeyError):
                pass
        return None

    def _binance_spot(self, pair: str) -> Optional[float]:
        """Binance spot price fallback."""
        symbol = _BINANCE_SYMBOL_MAP.get(pair, pair.replace("-", ""))
        data = self._http_get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
        if data and "price" in data:
            try:
                return float(data["price"])
            except (ValueError, KeyError):
                pass
        return None

    def get_buy_price(self, pair: str = "BTC-USD") -> Optional[float]:
        """Get current buy price (ask)."""
        data = self._http_get(f"{COINBASE_API}/prices/{pair}/buy")
        if data and "data" in data:
            try:
                return float(data["data"]["amount"])
            except (ValueError, KeyError):
                pass
        # Fallback: use spot
        return self.get_spot_price(pair)

    def get_sell_price(self, pair: str = "BTC-USD") -> Optional[float]:
        """Get current sell price (bid)."""
        data = self._http_get(f"{COINBASE_API}/prices/{pair}/sell")
        if data and "data" in data:
            try:
                return float(data["data"]["amount"])
            except (ValueError, KeyError):
                pass
        return self.get_spot_price(pair)

    def get_candles(self, product_id: str = "BTC-USD", granularity: int = 300, limit: int = 50) -> list:
        """
        Get OHLCV candles. Tries Coinbase Advanced Trade API, then Binance.
        Returns: list of [time, low, high, open, close, volume] (newest first).
        """
        cache_key = f"candles_{product_id}_{granularity}_{limit}"
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached["ts"] < max(granularity * 0.5, 30):
                return cached["data"]

        candles = self._coinbase_advanced_candles(product_id, granularity, limit)
        if not candles or len(candles) < 10:
            logger.debug(f"Coinbase candles insufficient ({len(candles) if candles else 0}), trying Binance...")
            candles = self._binance_candles(product_id, granularity, limit)

        if candles and len(candles) >= 2:
            self.cache[cache_key] = {"data": candles, "ts": time.time()}

        return candles or []

    def _coinbase_advanced_candles(self, product_id: str, granularity: int, limit: int) -> list:
        """Fetch candles from Coinbase Advanced Trade API (v3)."""
        gran_str = _GRANULARITY_MAP.get(granularity)
        if not gran_str:
            gran_str = "FIVE_MINUTE"

        now = int(time.time())
        start = now - (granularity * limit)

        url = (
            f"{COINBASE_ADVANCED}/market/products/{product_id}/candles"
            f"?start={start}&end={now}&granularity={gran_str}"
        )
        data = self._http_get(url)

        if data and "candles" in data:
            # v3 returns: {"candles": [{"start": ts, "low": x, "high": x, "open": x, "close": x, "volume": x}, ...]}
            candles = []
            for c in data["candles"]:
                try:
                    candles.append([
                        int(c["start"]),
                        float(c["low"]),
                        float(c["high"]),
                        float(c["open"]),
                        float(c["close"]),
                        float(c["volume"]),
                    ])
                except (KeyError, ValueError, TypeError):
                    continue
            # Sort newest first (highest timestamp first)
            candles.sort(key=lambda x: x[0], reverse=True)
            return candles[:limit]

        # Try the old exchange API as secondary fallback
        old_url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity={granularity}"
        data = self._http_get(old_url)
        if isinstance(data, list) and len(data) > 0:
            return data[:limit]

        return []

    def _binance_candles(self, product_id: str, granularity: int, limit: int) -> list:
        """Fetch candles from Binance (fallback). Converts to Coinbase format."""
        symbol = _BINANCE_SYMBOL_MAP.get(product_id, product_id.replace("-", ""))
        interval = _BINANCE_INTERVAL_MAP.get(granularity, "5m")

        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        data = self._http_get(url)

        if not isinstance(data, list):
            return []

        # Binance returns: [open_time, open, high, low, close, volume, ...]
        candles = []
        for k in data:
            try:
                candles.append([
                    int(k[0]) // 1000,  # ms -> seconds
                    float(k[3]),         # low
                    float(k[2]),         # high
                    float(k[1]),         # open
                    float(k[4]),         # close
                    float(k[5]),         # volume
                ])
            except (IndexError, ValueError, TypeError):
                continue

        # Sort newest first
        candles.sort(key=lambda x: x[0], reverse=True)
        return candles

    def get_ticker(self, product_id: str = "BTC-USD") -> Optional[dict]:
        """Get 24h ticker stats."""
        # Try Binance first (more reliable)
        symbol = _BINANCE_SYMBOL_MAP.get(product_id, product_id.replace("-", ""))
        data = self._http_get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}")
        if data and "lastPrice" in data:
            return {
                "price": float(data.get("lastPrice", 0)),
                "volume": float(data.get("volume", 0)),
                "bid": float(data.get("bidPrice", 0)),
                "ask": float(data.get("askPrice", 0)),
                "time": data.get("closeTime", ""),
            }
        # Coinbase fallback
        data = self._http_get(f"https://api.exchange.coinbase.com/products/{product_id}/ticker")
        if data and "price" in data:
            return {
                "price": float(data.get("price", 0)),
                "volume": float(data.get("volume", 0)),
                "bid": float(data.get("bid", 0)),
                "ask": float(data.get("ask", 0)),
                "time": data.get("time", ""),
            }
        return None

    def get_multi_prices(self, pairs: list = None) -> dict:
        """
        Get spot prices for multiple pairs.
        Default: BTC, ETH, SOL, MATIC (relevant to Polymarket/crypto markets).
        """
        pairs = pairs or ["BTC-USD", "ETH-USD", "SOL-USD", "MATIC-USD"]
        prices = {}
        for pair in pairs:
            price = self.get_spot_price(pair)
            if price is not None:
                prices[pair] = price
        return prices

    def get_price_momentum(self, product_id: str = "BTC-USD", periods: int = 12) -> Optional[dict]:
        """
        Compute short-term price momentum from 5-min candles.
        Returns direction, % change, and volatility.
        Useful for crypto-related prediction market signals.
        """
        candles = self.get_candles(product_id, granularity=300, limit=periods)
        if len(candles) < 2:
            return None

        # Candles are [time, low, high, open, close, volume] newest first
        closes = [c[4] for c in candles]
        latest = closes[0]
        oldest = closes[-1]

        pct_change = (latest - oldest) / oldest * 100 if oldest > 0 else 0

        # Simple volatility (std of returns)
        returns = []
        for i in range(len(closes) - 1):
            r = (closes[i] - closes[i + 1]) / closes[i + 1] * 100 if closes[i + 1] > 0 else 0
            returns.append(r)

        avg_return = sum(returns) / len(returns) if returns else 0
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns) if returns else 0
        volatility = variance ** 0.5

        return {
            "product": product_id,
            "latest_price": latest,
            "pct_change": round(pct_change, 3),
            "direction": "UP" if pct_change > 0.5 else "DOWN" if pct_change < -0.5 else "FLAT",
            "volatility": round(volatility, 3),
            "periods": periods,
            "candle_interval": "5min",
        }

    def is_connected(self) -> bool:
        """Test connectivity."""
        return self.get_spot_price("BTC-USD") is not None
