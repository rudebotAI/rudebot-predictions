"""
Price Tracker Sub-Bot -- Real-Time BTC Price Monitoring & Alert System
======================================================================
Autonomous sub-bot that continuously monitors BTC price action and generates
alerts when significant moves, breakouts, or regime changes are detected.

Runs as a lightweight thread inside the main bot. Feeds signals into the
CryptoMomentumEngine and can trigger out-of-cycle scans.

Features:
- Real-time price monitoring with configurable intervals
- Breakout detection (price crossing key levels)
- Volatility spike detection
- Price level tracking (support/resistance)
- Rolling statistics (TWAP, realized vol, min/max)
- Alert system for significant moves

Reusable: import PriceTracker for any asset tracking bot.
"""

import time
import math
import logging
import threading
from typing import Optional, Callable
from collections import deque

logger = logging.getLogger(__name__)


class PriceSnapshot:
    """Immutable price observation at a point in time."""
    __slots__ = ("timestamp", "price", "bid", "ask", "volume")

    def __init__(self, timestamp: float, price: float, bid: float = 0, ask: float = 0, volume: float = 0):
        self.timestamp = timestamp
        self.price = price
        self.bid = bid
        self.ask = ask
        self.volume = volume

    def spread(self) -> float:
        return self.ask - self.bid if self.ask and self.bid else 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "price": self.price,
            "bid": self.bid,
            "ask": self.ask,
            "volume": self.volume,
            "spread": self.spread(),
        }


class RollingStats:
    """
    Efficient rolling statistics over a price window.
    Uses a deque for O(1) append and O(n) stats -- suitable for windows up to ~10K.
    """

    def __init__(self, window_size: int = 720):
        """
        Args:
            window_size: Max observations to keep. Default 720 = 1 hour at 5s intervals.
        """
        self.window_size = window_size
        self._prices = deque(maxlen=window_size)
        self._timestamps = deque(maxlen=window_size)

    def add(self, price: float, timestamp: float = None):
        self._prices.append(price)
        self._timestamps.append(timestamp or time.time())

    @property
    def count(self) -> int:
        return len(self._prices)

    @property
    def latest(self) -> Optional[float]:
        return self._prices[-1] if self._prices else None

    @property
    def oldest(self) -> Optional[float]:
        return self._prices[0] if self._prices else None

    def mean(self) -> float:
        """TWAP (Time-Weighted Average Price) over the window."""
        if not self._prices:
            return 0
        return sum(self._prices) / len(self._prices)

    def std(self) -> float:
        """Standard deviation of prices in the window."""
        if len(self._prices) < 2:
            return 0
        m = self.mean()
        variance = sum((p - m) ** 2 for p in self._prices) / (len(self._prices) - 1)
        return variance ** 0.5

    def min_max(self) -> tuple:
        """(min_price, max_price) in the window."""
        if not self._prices:
            return (0, 0)
        return (min(self._prices), max(self._prices))

    def pct_change(self) -> float:
        """Percentage change from oldest to newest price."""
        if len(self._prices) < 2 or self._prices[0] <= 0:
            return 0
        return (self._prices[-1] - self._prices[0]) / self._prices[0] * 100

    def realized_volatility(self) -> float:
        """
        Annualized realized volatility from log returns.
        Standard institutional metric.
        """
        if len(self._prices) < 10:
            return 0
        log_returns = []
        prices = list(self._prices)
        for i in range(1, len(prices)):
            if prices[i] > 0 and prices[i - 1] > 0:
                log_returns.append(math.log(prices[i] / prices[i - 1]))
        if len(log_returns) < 2:
            return 0
        mean_r = sum(log_returns) / len(log_returns)
        var = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        # Annualize (assume 5s intervals -> 17280 per day -> 6.3M per year)
        interval_secs = 5
        if len(self._timestamps) >= 2:
            interval_secs = max(1, (self._timestamps[-1] - self._timestamps[0]) / max(len(self._timestamps) - 1, 1))
        periods_per_year = 365.25 * 24 * 3600 / interval_secs
        return round((var ** 0.5) * (periods_per_year ** 0.5) * 100, 2)

    def summary(self) -> dict:
        mn, mx = self.min_max()
        return {
            "count": self.count,
            "twap": round(self.mean(), 2),
            "std": round(self.std(), 2),
            "min": round(mn, 2),
            "max": round(mx, 2),
            "pct_change": round(self.pct_change(), 4),
            "realized_vol_ann": self.realized_volatility(),
            "latest": round(self.latest, 2) if self.latest else 0,
        }


class PriceTracker:
    """
    Autonomous price monitoring sub-bot.
    Runs in a background thread, collecting price data and generating alerts.
    """

    def __init__(self, coinbase_connector, config: dict = None):
        config = config or {}
        self.coinbase = coinbase_connector
        self.product = config.get("product", "BTC-USD")
        self.poll_interval = config.get("poll_interval_sec", 10)
        self.alert_threshold_pct = config.get("alert_threshold_pct", 1.0)
        self.breakout_lookback = config.get("breakout_lookback", 60)  # observations

        # Rolling stats windows
        self.short_window = RollingStats(window_size=60)   # ~5 min at 5s
        self.medium_window = RollingStats(window_size=720)  # ~1 hour at 5s
        self.long_window = RollingStats(window_size=4320)   # ~6 hours at 5s

        # Key price levels for breakout detection
        self._key_levels = []
        self._alerts = deque(maxlen=100)

        # Callback for when alerts fire
        self._on_alert: Optional[Callable] = None

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def set_alert_callback(self, callback: Callable):
        """Register a callback function for price alerts. Signature: callback(alert_dict)."""
        self._on_alert = callback

    def add_key_level(self, price: float, label: str = ""):
        """Add a key price level for breakout detection."""
        self._key_levels.append({"price": price, "label": label, "alerted": False})
        self._key_levels.sort(key=lambda x: x["price"])

    def clear_key_levels(self):
        self._key_levels = []

    def start(self):
        """Start the price tracker in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"PriceTracker-{self.product}")
        self._thread.start()
        logger.info(f"[PRICE_TRACKER] Started for {self.product} (poll every {self.poll_interval}s)")

    def stop(self):
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"[PRICE_TRACKER] Stopped for {self.product}")

    def _run_loop(self):
        """Main polling loop (runs in background thread)."""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.debug(f"[PRICE_TRACKER] Poll error: {e}")
            time.sleep(self.poll_interval)

    def _poll_once(self):
        """Single price poll cycle."""
        ticker = self.coinbase.get_ticker(self.product)
        if not ticker:
            return

        now = time.time()
        price = ticker.get("price", 0)
        bid = ticker.get("bid", 0)
        ask = ticker.get("ask", 0)
        volume = ticker.get("volume", 0)

        if price <= 0:
            return

        snap = PriceSnapshot(now, price, bid, ask, volume)

        # Update all windows
        self.short_window.add(price, now)
        self.medium_window.add(price, now)
        self.long_window.add(price, now)

        # Check for alerts
        self._check_move_alert(snap)
        self._check_breakout_alert(snap)
        self._check_volatility_spike()

    def _check_move_alert(self, snap: PriceSnapshot):
        """Alert on significant price moves within the short window."""
        if self.short_window.count < 10:
            return
        pct = abs(self.short_window.pct_change())
        if pct >= self.alert_threshold_pct:
            direction = "UP" if self.short_window.pct_change() > 0 else "DOWN"
            alert = {
                "type": "significant_move",
                "product": self.product,
                "direction": direction,
                "pct_change": round(self.short_window.pct_change(), 3),
                "price": snap.price,
                "timestamp": snap.timestamp,
                "window": "5min",
            }
            self._fire_alert(alert)

    def _check_breakout_alert(self, snap: PriceSnapshot):
        """Alert when price crosses a key level."""
        prev = self.short_window.oldest
        if prev is None:
            return

        for level in self._key_levels:
            lp = level["price"]
            if level["alerted"]:
                continue
            # Check if price crossed the level
            if (prev < lp <= snap.price) or (prev > lp >= snap.price):
                direction = "ABOVE" if snap.price >= lp else "BELOW"
                alert = {
                    "type": "breakout",
                    "product": self.product,
                    "level": lp,
                    "label": level["label"],
                    "direction": direction,
                    "price": snap.price,
                    "timestamp": snap.timestamp,
                }
                level["alerted"] = True
                self._fire_alert(alert)

    def _check_volatility_spike(self):
        """Alert when short-term vol significantly exceeds medium-term vol."""
        if self.short_window.count < 20 or self.medium_window.count < 100:
            return
        short_vol = self.short_window.std()
        medium_vol = self.medium_window.std()
        if medium_vol > 0 and short_vol / medium_vol > 2.0:
            alert = {
                "type": "volatility_spike",
                "product": self.product,
                "short_vol": round(short_vol, 2),
                "medium_vol": round(medium_vol, 2),
                "ratio": round(short_vol / medium_vol, 2),
                "timestamp": time.time(),
            }
            self._fire_alert(alert)

    def _fire_alert(self, alert: dict):
        """Process and dispatch an alert."""
        self._alerts.append(alert)
        logger.info(f"[PRICE_ALERT] {alert['type']}: {self.product} | {alert}")
        if self._on_alert:
            try:
                self._on_alert(alert)
            except Exception as e:
                logger.debug(f"Alert callback error: {e}")

    def get_alerts(self, limit: int = 20) -> list:
        """Get recent alerts."""
        return list(self._alerts)[-limit:]

    def get_status(self) -> dict:
        """Get current tracker state."""
        return {
            "product": self.product,
            "running": self._running,
            "short_window": self.short_window.summary(),
            "medium_window": self.medium_window.summary(),
            "long_window": self.long_window.summary(),
            "key_levels": len(self._key_levels),
            "recent_alerts": len(self._alerts),
        }
