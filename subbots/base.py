"""
Bot Skeleton Framework -- Reusable Base Classes for Future Bot Projects
=======================================================================
Provides the foundational architecture for building any trading or
monitoring bot. All bots in this system inherit from these base classes.

Architecture pattern:
    BaseBot (lifecycle, config, logging)
        └── BaseSubBot (threading, start/stop, health checks)
             ├── PriceTracker
             ├── NewsSentinel
             └── [your future sub-bot]

    BaseEngine (stateless compute, indicators)
        ├── CryptoMomentumEngine
        ├── EVScanner
        └── [your future engine]

    BaseConnector (API client, auth, rate limiting)
        ├── KalshiConnector
        ├── CoinbaseConnector
        └── [your future connector]

Usage:
    from subbots.base import BaseBot, BaseSubBot, BaseEngine, BaseConnector

    class MyNewBot(BaseBot):
        def _tick(self):
            # Your scan logic here
            pass

    class MyDataFeed(BaseSubBot):
        def _poll_once(self):
            # Your data collection here
            pass
"""

import time
import signal
import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional, Callable
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class BaseConnector(ABC):
    """
    Base class for all API connectors.
    Provides: rate limiting, auth management, error tracking.

    Subclass and implement:
        - is_connected() -> bool
        - Specific API methods
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self._config = config
        self._last_request = 0
        self._min_interval = config.get("rate_limit_sec", 0.5)
        self._errors = deque(maxlen=50)
        self._request_count = 0

    def _throttle(self):
        """Rate limiter -- call before every API request."""
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()
        self._request_count += 1

    def _record_error(self, error: str):
        self._errors.append({"time": time.time(), "error": str(error)[:200]})

    @abstractmethod
    def is_connected(self) -> bool:
        """Test API connectivity."""
        ...

    def get_stats(self) -> dict:
        return {
            "requests": self._request_count,
            "errors": len(self._errors),
            "recent_errors": list(self._errors)[-5:],
        }


class BaseEngine(ABC):
    """
    Base class for all compute/analysis engines.
    Engines are stateless -- feed data in, get analysis out.

    Subclass and implement:
        - analyze(data) -> dict
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._cache = {}
        self._cache_ttl = self._config.get("cache_ttl", 60)

    def _get_cached(self, key: str) -> Optional[dict]:
        if key in self._cache:
            entry = self._cache[key]
            if time.time() - entry["ts"] < self._cache_ttl:
                return entry["data"]
        return None

    def _set_cached(self, key: str, data: dict):
        self._cache[key] = {"data": data, "ts": time.time()}

    @abstractmethod
    def analyze(self, data) -> dict:
        """Run analysis on input data. Returns results dict."""
        ...


class BaseSubBot(ABC):
    """
    Base class for all background sub-bots.
    Provides: threading, start/stop lifecycle, health monitoring.

    Subclass and implement:
        - _poll_once() -- called each cycle
        - get_status() -> dict -- return current state
    """

    def __init__(self, name: str, config: dict = None):
        config = config or {}
        self.name = name
        self._config = config
        self.poll_interval = config.get("poll_interval_sec", 30)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._tick_count = 0
        self._errors = deque(maxlen=50)
        self._on_error: Optional[Callable] = None

    def start(self):
        """Start the sub-bot in a daemon thread."""
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"SubBot-{self.name}"
        )
        self._thread.start()
        logger.info(f"[{self.name}] Started (poll every {self.poll_interval}s)")

    def stop(self):
        """Gracefully stop the sub-bot."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info(f"[{self.name}] Stopped after {self._tick_count} ticks")

    def _run_loop(self):
        while self._running:
            try:
                self._poll_once()
                self._tick_count += 1
            except Exception as e:
                self._errors.append({"time": time.time(), "error": str(e)[:200]})
                logger.warning(f"[{self.name}] Error: {e}")
                if self._on_error:
                    self._on_error(self.name, e)
            time.sleep(self.poll_interval)

    @abstractmethod
    def _poll_once(self):
        """One cycle of the sub-bot's work. Implement in subclass."""
        ...

    @abstractmethod
    def get_status(self) -> dict:
        """Return current status dict. Implement in subclass."""
        ...

    def health_check(self) -> dict:
        """Standard health check for monitoring."""
        uptime = time.time() - self._started_at if self._started_at else 0
        return {
            "name": self.name,
            "running": self._running,
            "uptime_sec": round(uptime),
            "ticks": self._tick_count,
            "errors": len(self._errors),
            "last_error": self._errors[-1] if self._errors else None,
        }


class BaseBot(ABC):
    """
    Base class for the main bot orchestrator.
    Provides: config loading, scan loop, graceful shutdown,
    sub-bot management, and error reporting.

    Subclass and implement:
        - _init_components(config) -- create connectors, engines, sub-bots
        - _tick() -- one scan cycle
        - _cleanup() -- shutdown logic

    Usage:
        class MyBot(BaseBot):
            def _init_components(self, config):
                self.connector = SomeConnector(config)
                self.register_subbot(SomeSubBot(config))

            def _tick(self):
                data = self.connector.fetch()
                # process...

        bot = MyBot(config)
        bot.run_loop()  # or bot.run_once()
    """

    def __init__(self, config: dict):
        self.config = config
        self.mode = config.get("mode", "paper")
        self.scan_interval = config.get("strategy", {}).get("scan_interval_sec", 120)
        self.running = True
        self.scan_number = 0
        self.errors = deque(maxlen=100)
        self._subbots: list = []

        self._init_components(config)

    @abstractmethod
    def _init_components(self, config: dict):
        """Initialize all connectors, engines, and sub-bots."""
        ...

    @abstractmethod
    def _tick(self):
        """One full scan cycle. Called every scan_interval seconds."""
        ...

    @abstractmethod
    def _cleanup(self):
        """Cleanup on shutdown."""
        ...

    def register_subbot(self, subbot: BaseSubBot):
        """Register a sub-bot for lifecycle management."""
        self._subbots.append(subbot)

    def start_subbots(self):
        """Start all registered sub-bots."""
        for sb in self._subbots:
            sb.start()

    def stop_subbots(self):
        """Stop all registered sub-bots."""
        for sb in self._subbots:
            sb.stop()

    def subbot_health(self) -> list:
        """Get health status of all sub-bots."""
        return [sb.health_check() for sb in self._subbots]

    def run_loop(self):
        """Main loop -- runs until interrupted."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.start_subbots()

        while self.running:
            try:
                self._tick()
                self.scan_number += 1
                time.sleep(self.scan_interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.errors.append({"time": time.time(), "error": str(e)[:200]})
                logger.error(f"Loop error: {e}", exc_info=True)
                time.sleep(10)

        self.stop_subbots()
        self._cleanup()

    def run_once(self):
        """Single scan cycle -- useful for testing."""
        self.start_subbots()
        time.sleep(2)  # Let sub-bots collect initial data
        self._tick()
        self.stop_subbots()
        self._cleanup()

    def _handle_signal(self, *args):
        logger.info("Shutdown signal received")
        self.running = False

    def get_bot_status(self) -> dict:
        """Full bot status including all sub-bots."""
        return {
            "mode": self.mode,
            "running": self.running,
            "scan_number": self.scan_number,
            "scan_interval": self.scan_interval,
            "errors": len(self.errors),
            "subbots": self.subbot_health(),
        }
