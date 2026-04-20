"""
On-Chain Whale Signal (U2)

Subscribes to Polygon blocks and filters for transactions from tracked
whale addresses interacting with the Polymarket CLOB exchange contract.
Decodes calldata (token ID, side, size) and emits a signal 3–30 seconds
ahead of the public positions API.

Feeds the same signal bus as copy_trader. Candidates are filtered through
the EV + Kelly stack before being sent to Telegram for confirmation.

Safety:
  * enabled: False by default — opt-in.
  * This module only PRODUCES signals; it does not place orders.
  * Requires websockets + eth_abi. Add to requirements.txt when adopting.
  * Telegram confirmation remains the sole path from signal to trade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

# Polymarket CLOB Exchange on Polygon (mainnet). Verify before enabling.
POLYMARKET_CLOB_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e".lower()

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "rpc_ws": "wss://polygon-rpc.com",   # user must override with a reliable WS endpoint
    "whales": [],                        # list of 0x addresses
    "min_usd": 500.0,                    # only emit on trades above this notional
    "reconnect_backoff_sec": 5,
    "max_reconnect_backoff_sec": 60,
}


@dataclass
class WhaleSignal:
    tx_hash: str
    whale: str
    market_id: str
    side: str
    size_shares: float
    price: float
    usd: float


class PolygonWhaleWatcher:
    """Async Polygon block listener. Emits WhaleSignal objects via `on_signal`."""

    def __init__(self, cfg: Dict[str, Any], on_signal: Callable[[WhaleSignal], None]):
        merged = {**DEFAULTS, **(cfg.get("whale") or {})}
        self.cfg = merged
        self.on_signal = on_signal
        self._stop = asyncio.Event()
        self._whales = {w.lower() for w in merged.get("whales", [])}

    async def run(self) -> None:
        if not self.cfg.get("enabled", False):
            log.info("polygon_whale disabled")
            return
        backoff = self.cfg["reconnect_backoff_sec"]
        try:
            import websockets  # imported lazily so the module loads even without the dep
        except ImportError:
            log.error("polygon_whale requires the 'websockets' package")
            return

        while not self._stop.is_set():
            try:
                async with websockets.connect(self.cfg["rpc_ws"]) as ws:
                    await self._subscribe(ws)
                    backoff = self.cfg["reconnect_backoff_sec"]
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle_message(raw)
            except Exception as e:  # noqa: BLE001
                log.warning("polygon_whale: WS error: %s; reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg["max_reconnect_backoff_sec"])

    def stop(self) -> None:
        self._stop.set()

    # ---- internal ---------------------------------------------------------
    async def _subscribe(self, ws) -> None:
        # Subscribe to newPendingTransactions for fastest signal.
        await ws.send(json.dumps({
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_subscribe",
            "params": ["newPendingTransactions", True],  # full-tx subscription; some RPCs reject
        }))

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        params = msg.get("params") or {}
        result = params.get("result") or {}
        to = (result.get("to") or "").lower()
        frm = (result.get("from") or "").lower()
        if not to or to != POLYMARKET_CLOB_EXCHANGE:
            return
        if frm not in self._whales:
            return
        signal = self._decode_calldata(result)
        if signal is None:
            return
        if signal.usd < self.cfg["min_usd"]:
            return
        log.info("polygon_whale: detected whale trade tx=%s usd=%.2f", signal.tx_hash, signal.usd)
        self.on_signal(signal)

    def _decode_calldata(self, tx: Dict[str, Any]) -> Optional[WhaleSignal]:
        """Decode the Polymarket CLOB fillOrder/matchOrders calldata.

        IMPORTANT: Stubbed. Real implementation must decode the exact ABI
        of the Polymarket exchange. Load the ABI from a trusted source
        (e.g. poly-clob-client) and use eth_abi.decode. Leaving this as a
        defensive no-op keeps the scaffold safe until properly implemented.
        """
        _ = tx
        return None
