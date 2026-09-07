# Pre-registration — K1: sports final-minutes leader

Registered: 2026-09-06. Status: **registered, not yet run** (backtest first).
Evidence: *Prices, Probabilities, and Parlays* (arXiv 2607.14430, 23M Kalshi
NBA/MLB/NHL moneyline trades, Mar–May 2026): calibration deteriorates sharply
in the final ~10 minutes of a game, consistent with holders of the losing side
buying insurance — the leading side is temporarily underpriced.

## Frozen rule

| Element | Value |
|---|---|
| Universe | Kalshi single-game moneyline series for NBA, MLB, NHL (no parlays/KXMVE) |
| Window | scheduled resolution within 10 minutes, market still open |
| Signal | calibrated P(leader) − our resting bid ≥ 0.04, using the `Sports|1` cell only if usable, else the game-time reliability table estimated in the backtest (frozen at registration of the run) |
| Side | the side priced ≥ 0.60 (the leader); never the trailer |
| Entry | post-only bid at the leader's bid; rest until the market closes; no chasing |
| Exit | hold to settlement (no stop; a $10 position at 0.80 risks $8) |
| Size | ¼-Kelly on the calibrated edge, capped at `max_position_usd` |

## Backtest (before any paper trading)

Data: `GET /markets/trades` for settled games in the three series over ≥ 60
days; reconstruct the best bid in the final 10 minutes from trades
(taker_book_side) — conservative: assume our bid fills only when a later trade
prints at or below it.

Pass = (1) net return > 0 in ≥ 3 of 4 fortnightly slices at 0 maker fee,
(2) Brier of the calibrated leader probability beats the market price on the
last 30% of games chronologically, (3) conservative fill rate ≥ 25%,
(4) positive net return still holds if fills are haircut 50%.

Fail = any gate fails; K1 is closed and not re-parameterised.
