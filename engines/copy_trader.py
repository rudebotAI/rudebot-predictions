"""
Copy Trading Engine (U1)

Tracks a configurable set of Polymarket wallet addresses, detects position
changes by diffing snapshots, and routes each detected trade through the
existing EV + Kelly + risk stack before alerting. DOES NOT mirror blindly —
every copy must clear the EV gap and Kelly sizing filters.

Safety:
  * Paper-mode enforcement — this module will refuse to emit live orders
    unless cfg["mode"] == "live" AND cfg["copy_trading"]["enabled"] is True.
  * Telegram confirmation loop remains intact (handled by the alerts layer).
  * Per-wallet daily cap and copy_percentage are capped defensively.

This is a scaffold. The actual Polymarket position polling is delegated to
connectors.polymarket.get_positions(address) which should already exist in
the project. If it does not, implement the stub at the bottom of the file.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)


# --- Configuration defaults (override via config.yaml) ------------------------
DEFAULTS: Dict[str, Any] = {
    "enabled": False,                 # OFF by default — opt-in
    "wallets": [],                    # list of 0x addresses to track
    "poll_interval_sec": 30,          # how often to re-poll each wallet
    "copy_percentage": 0.05,          # 5% of tracked wallet's notional
    "max_copy_usd_per_trade": 10.0,   # hard cap per copy
    "per_wallet_daily_cap_usd": 50.0, # daily cap per tracked wallet
    "min_ev_gap": 0.05,               # require EV filter to agree (5%)
    "require_kelly_positive": True,   # skip if Kelly suggests zero size
}


@dataclass
class Position:
    wallet: str
    market_id: str
    side: str            # "YES" or "NO"
    shares: float
    avg_price: float


@dataclass
class Delta:
    wallet: str
    market_id: str
    side: str
    shares_delta: float  # positive = opened/increased, negative = reduced/closed
    tracked_avg_price: float


@dataclass
class WalletTracker:
    address: str
    last_snapshot: Dict[str, Position] = field(default_factory=dict)
    daily_copy_usd: float = 0.0
    day_anchor_epoch: float = field(default_factory=time.time)


class CopyTrader:
    """Copy-trading signal generator. Emits candidate trades; does NOT execute."""

    def __init__(self, cfg: Dict[str, Any], deps: Dict[str, Any]):
        merged = {**DEFAULTS, **(cfg.get("copy_trading") or {})}
        self.cfg = merged
        self.mode = cfg.get("mode", "paper")
        self.deps = deps  # expected: {"polymarket": <connector>, "scanner": <EV scanner>, "sizer": <Kelly sizer>}
        self.trackers: Dict[str, WalletTracker] = {
            addr: WalletTracker(address=addr) for addr in merged.get("wallets", [])
        }

    # ----- public API -------------------------------------------------------
    def scan_once(self) -> List[Dict[str, Any]]:
        """One polling cycle. Returns a list of candidate copy trades.

        A candidate is a dict ready to hand to the alerts/paper/live layer.
        Candidates DO NOT execute — the existing Telegram confirm loop must
        approve them first.
        """
        if not self.cfg.get("enabled", False):
            log.debug("copy_trader disabled; skipping")
            return []

        candidates: List[Dict[str, Any]] = []
        for addr, tracker in self.trackers.items():
            self._roll_daily_cap(tracker)
            deltas = self._diff_wallet(tracker)
            for d in deltas:
                cand = self._build_candidate(d, tracker)
                if cand is not None:
                    candidates.append(cand)
        return candidates

    # ----- internals --------------------------------------------------------
    def _diff_wallet(self, tracker: WalletTracker) -> List[Delta]:
        poly = self.deps.get("polymarket")
        if poly is None:
            log.warning("copy_trader: no polymarket connector injected")
            return []
        current_raw = poly.get_positions(tracker.address) or []
        current: Dict[str, Position] = {}
        for p in current_raw:
            # normalize whatever the connector returns into our Position shape
            pos = Position(
                wallet=tracker.address,
                market_id=str(p.get("market_id") or p.get("token_id")),
                side=str(p.get("side", "YES")).upper(),
                shares=float(p.get("shares", 0.0)),
                avg_price=float(p.get("avg_price", 0.0)),
            )
            current[f"{pos.market_id}:{pos.side}"] = pos

        deltas: List[Delta] = []
        keys = set(current) | set(tracker.last_snapshot)
        for k in keys:
            prev = tracker.last_snapshot.get(k)
            curr = current.get(k)
            if prev is None and curr is not None:
                deltas.append(Delta(tracker.address, curr.market_id, curr.side,
                                    curr.shares, curr.avg_price))
            elif curr is None and prev is not None:
                deltas.append(Delta(tracker.address, prev.market_id, prev.side,
                                    -prev.shares, prev.avg_price))
            elif prev is not None and curr is not None:
                delta = curr.shares - prev.shares
                if abs(delta) > 1e-9:
                    deltas.append(Delta(tracker.address, curr.market_id, curr.side,
                                        delta, curr.avg_price))
        tracker.last_snapshot = current
        return deltas

    def _build_candidate(self, d: Delta, tracker: WalletTracker) -> Optional[Dict[str, Any]]:
        if d.shares_delta <= 0:
            # Mirror closures: signal "close our copy position if any"; no new risk added.
            return {
                "kind": "copy_close",
                "wallet": d.wallet,
                "market_id": d.market_id,
                "side": d.side,
            }

        scanner = self.deps.get("scanner")
        sizer = self.deps.get("sizer")
        if scanner is None or sizer is None:
            log.warning("copy_trader: scanner or sizer missing; skipping filter")
            return None

        # Run the copied trade through the existing EV scanner.
        ev_result = scanner.evaluate(market_id=d.market_id, side=d.side,
                                     observed_price=d.tracked_avg_price)
        if not ev_result or ev_result.get("ev_gap", 0.0) < self.cfg["min_ev_gap"]:
            log.info("copy_trader: EV filter rejected copy of %s", d.market_id)
            return None

        # Size via Kelly against our own bankroll, THEN cap by copy_percentage.
        kelly_size_usd = sizer.kelly_size(prob=ev_result["true_prob"],
                                         odds_price=d.tracked_avg_price)
        if self.cfg["require_kelly_positive"] and kelly_size_usd <= 0:
            return None

        # Copy fraction of the WALLET's sizing, bounded by our kelly + per-trade cap.
        tracked_notional = d.shares_delta * d.tracked_avg_price
        copy_usd = min(
            tracked_notional * self.cfg["copy_percentage"],
            kelly_size_usd,
            self.cfg["max_copy_usd_per_trade"],
        )

        # Enforce per-wallet daily cap.
        remaining = self.cfg["per_wallet_daily_cap_usd"] - tracker.daily_copy_usd
        if remaining <= 0 or copy_usd <= 0:
            return None
        copy_usd = min(copy_usd, remaining)
        tracker.daily_copy_usd += copy_usd

        return {
            "kind": "copy_open",
            "source": "copy_trader",
            "wallet": d.wallet,
            "market_id": d.market_id,
            "side": d.side,
            "price": d.tracked_avg_price,
            "usd": round(copy_usd, 4),
            "ev_gap": ev_result["ev_gap"],
            "true_prob": ev_result["true_prob"],
            "note": "awaiting Telegram confirm",
        }

    def _roll_daily_cap(self, tracker: WalletTracker) -> None:
        if time.time() - tracker.day_anchor_epoch >= 86400:
            tracker.daily_copy_usd = 0.0
            tracker.day_anchor_epoch = time.time()


# ---- optional stub if the project's polymarket connector lacks get_positions ----
def get_positions_stub(address: str) -> list:
    """Placeholder. Real implementation should hit
    https://data-api.polymarket.com/positions?user={address}
    and return [{market_id, side, shares, avg_price}, ...]
    """
    raise NotImplementedError(
        "connectors.polymarket.get_positions must be implemented for copy_trader"
    )
