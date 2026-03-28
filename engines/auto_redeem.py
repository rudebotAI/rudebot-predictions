"""
Auto-Redeem Engine -- Background Winnings Collection
=====================================================
Periodically checks for redeemable positions on Polymarket
and collects winnings automatically.

On Polymarket, winning positions must be actively redeemed
(merged/burned) to collect USDC. This runs as a background
thread checking every N minutes.

Reference: txbabaxyz/polyterminal redeemall.py + 4coinsbot simple_redeem_collector.py
"""

import time
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class AutoRedeemer:
    """
    Background service that automatically redeems winning positions.

    Features:
    - Periodic scanning for redeemable positions
    - Batch redemption (process all at once)
    - Telegram notifications on successful redeems
    - File-based lock to prevent concurrent redeems
    - Statistics tracking

    Usage:
        redeemer = AutoRedeemer(poly_connector, config)
        redeemer.start()  # Background thread
        ...
        redeemer.stop()
    """

    def __init__(self, polymarket_connector, config: dict = None, alerts=None):
        config = config or {}
        self.poly = polymarket_connector
        self.alerts = alerts
        self.enabled = config.get("enabled", True)
        self.interval_sec = config.get("redeem_interval_sec", 300)  # Check every 5 min
        self.min_redeem_usd = config.get("min_redeem_usd", 0.50)  # Skip dust

        self._running = False
        self._thread = None
        self._lock_file = "logs/.redeem_lock"
        self._stats = {
            "total_redeemed_usd": 0.0,
            "total_redeems": 0,
            "last_redeem_time": None,
            "errors": 0,
        }

    def start(self):
        """Start background redeem thread."""
        if not self.enabled:
            logger.info("Auto-redeem disabled")
            return

        if not self.poly.private_key:
            logger.info("Auto-redeem: no Polymarket wallet key -- disabled")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._redeem_loop,
            daemon=True,
            name="auto-redeemer",
        )
        self._thread.start()
        logger.info(f"Auto-redeem started (checking every {self.interval_sec}s)")

    def stop(self):
        """Stop background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Auto-redeem stopped")

    def _redeem_loop(self):
        """Main background loop."""
        while self._running:
            try:
                self._check_and_redeem()
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning(f"Auto-redeem error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(self.interval_sec):
                if not self._running:
                    break
                time.sleep(1)

    def _check_and_redeem(self):
        """Check for and redeem winning positions."""
        # Acquire lock
        if not self._acquire_lock():
            logger.debug("Auto-redeem: another process holds the lock")
            return

        try:
            positions = self.poly.get_positions()
            if not positions:
                return

            redeemable = []
            for pos in positions:
                # Check if position is resolved and has winnings
                # Polymarket positions that resolved in your favor can be redeemed
                outcome = pos.get("outcome")
                size = float(pos.get("size", 0))
                if outcome and size > 0:
                    value_usd = size  # Each winning share pays $1
                    if value_usd >= self.min_redeem_usd:
                        redeemable.append({
                            "condition_id": pos.get("conditionId", ""),
                            "token_id": pos.get("tokenId", ""),
                            "size": size,
                            "value_usd": value_usd,
                        })

            if not redeemable:
                return

            logger.info(f"Auto-redeem: {len(redeemable)} positions to redeem")

            total_redeemed = 0
            for r in redeemable:
                try:
                    success = self._redeem_position(r)
                    if success:
                        total_redeemed += r["value_usd"]
                        self._stats["total_redeems"] += 1
                except Exception as e:
                    logger.warning(f"Redeem failed for {r['condition_id'][:16]}: {e}")
                    self._stats["errors"] += 1

            if total_redeemed > 0:
                self._stats["total_redeemed_usd"] += total_redeemed
                self._stats["last_redeem_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                logger.info(f"Auto-redeem: collected ${total_redeemed:.2f}")

                if self.alerts and self.alerts.is_configured():
                    self.alerts.send(
                        f"<b>Auto-Redeem</b>\n"
                        f"Collected: ${total_redeemed:.2f}\n"
                        f"Positions: {len(redeemable)}"
                    )

        finally:
            self._release_lock()

    def _redeem_position(self, position: dict) -> bool:
        """
        Redeem a single position.
        Uses py-clob-client merge/burn operations.
        """
        if not self.poly.client:
            if not self.poly._init_client():
                return False

        try:
            # The exact redeem mechanism depends on the market type
            # For standard markets: merge YES+NO tokens back to USDC
            # For negRisk markets: different call
            condition_id = position["condition_id"]

            # Try standard merge first
            result = self.poly.client.merge_positions(condition_id)
            if result:
                logger.info(f"Redeemed: {condition_id[:16]}... | ${position['value_usd']:.2f}")
                return True

            return False
        except Exception as e:
            logger.debug(f"Redeem error: {e}")
            return False

    def _acquire_lock(self) -> bool:
        """File-based lock to prevent concurrent redeems."""
        import os
        try:
            fd = os.open(self._lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if lock is stale (>10 min old)
            try:
                age = time.time() - os.path.getmtime(self._lock_file)
                if age > 600:
                    os.unlink(self._lock_file)
                    return self._acquire_lock()
            except Exception:
                pass
            return False
        except Exception:
            return True  # If file ops fail, proceed anyway

    def _release_lock(self):
        """Release file lock."""
        try:
            import os
            os.unlink(self._lock_file)
        except Exception:
            pass

    def force_redeem(self) -> dict:
        """Manually trigger a redeem cycle. Returns stats."""
        self._check_and_redeem()
        return self._stats.copy()

    def get_stats(self) -> dict:
        """Get redeem statistics."""
        return {
            **self._stats,
            "enabled": self.enabled,
            "interval_sec": self.interval_sec,
            "running": self._running,
        }
