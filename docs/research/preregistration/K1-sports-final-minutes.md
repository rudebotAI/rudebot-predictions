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

## Result — 2026-09-07 (one run; K1 is closed)

Data: 774 settled MLB moneyline games (Jul 8 – Sep 6 2026), full trade tape
of the final 30–75 minutes from `GET /markets/trades`, MLB Stats API
play-by-play for inning anchors. NBA/NHL: off-season, 0 games.
Report: `docs/research/k1_report_inning9.json`.

| Variant | attempted | filled | fill rate | win rate | net / $ | fortnights > 0 |
|---|---|---|---|---|---|---|
| "10 min before close" (ex-post anchor) | 739 | 293 | 40% | 95.6% | **+9.9%** | 5/5 |
| **Start of 9th inning (live-implementable)** | 677 | 317 | 47% | 92.1% | **+1.4%** | 4/5 |
| Start of bottom 9th (robustness) | 375 | 183 | 49% | 85.8% | +2.1% | 4/5 |

The mechanism in the paper is visible: the leader's 10-minute price resolves
above price in every bucket (0.85–0.95 → 98.8%, n=163). But the +9.9% is not
harvestable: anchoring to Kalshi's close time conditions on the game ending
within ten minutes, i.e. on the leader closing it out — information nobody has
at trade time. With a trigger the bot can actually use (top of the 9th from
the MLB Stats API) the net edge is +1.4% per dollar, t ≈ 0.9 (per-fill SD ≈
0.30, n = 317): not distinguishable from zero, and worth ≈ 13¢ per $10 fill.

**Verdict: FAIL** (gate `net_gt_2se`). K1 is closed; no re-parameterisation.
What survives as knowledge: (a) maker fills in these markets are realistic
(~47% strict fill rate), (b) MLB series carry a 0.5× maker fee (fee_type
`quadratic_with_maker_fees`) — now modelled per series in the bot, (c) the
ex-post-anchor trap is documented so the next candidate does not fall into it.
