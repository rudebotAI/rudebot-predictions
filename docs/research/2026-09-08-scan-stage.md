# 2026-09-08 — Why an armed model traded nothing, and what the fix revealed

## Symptom

v6.1 deployed 2026-09-07 with three usable calibration cells
(`Science and Technology|24`, `Science and Technology|72`, `Sports|24`).
After 735 scan cycles: 0 opportunities, 0 resting orders.

## Cause

The Scan stage was model-blind. `scan_markets_with_prices` reads the first
100 pages of `GET /events?status=open` and the top-volume markets of each
event. Open events are dominated by long-dated Elections / Politics series;
everything it returned sat in the 168 h horizon bucket in categories with no
usable cell. The Predict stage then (correctly) returned the market price for
every market, i.e. edge 0.

## Fix (v6.2): the scan follows the model

`CalibratedModel.scan_targets()` turns each usable cell into a search
specification: category, frequency class and an hours-to-resolution window
(geometric midpoints between fitted horizons, so a market is fetched exactly
when it would be priced by that cell).

`KalshiConnector.discover_targets()` (background thread, every 20 min) uses
the daily series catalog and a 15-min open-event index to select the series
that match a cell, and lists their open markets whose
`expected_expiration_time` falls in the window. `close_time` is not used for
the horizon: Kalshi rewrites it when a market closes early, and the server-
side `min/max_close_ts` filter is only a coarse pre-filter here. The call
budget is round-robined across cells so Sports cannot starve a small
category. `requote_candidates()` refreshes the survivors every cycle with one
call per event.

Live probe 2026-09-08 17:20 UTC (public API, no auth): catalog 13,867 series
(cached), open-event index 3,930 series in 18 s, discovery 150 series calls
in 51 s → 32 in-window, future-dated `Sports/recurring|24` markets (CS2, MLB
RBI props, NPB, T20); requote of 20 events in 5 s; 0 stale rows.

## What the finer cells revealed

The v6.1 `Sports|24` cell (slope 0.735, "market overconfident") pooled game
markets with season futures and awards. Re-keying cells by
`Category/freq_class|horizon` (recurring = daily/hourly/weekly/15-min/custom
series, one_off = everything else) and refitting the 60-day dataset:

| cell | n | slope | 95% CI | holdout Brier mkt → cal | verdict |
|---|---|---|---|---|---|
| Sports/recurring\|24 | 517 | 1.01 | [0.94, 1.51] | 0.2008 → 0.2043 | not used: no improvement |
| Sports/recurring\|6 | 531 | 1.07 | [1.01, 1.54] | 0.1797 → 0.1825 | not used: no improvement |
| Sports/one_off\|24 | 133 | 0.87 | [0.26, 2.21] | 0.2341 → 0.2056 | not used: n < 150 |
| Science and Technology/one_off\|24 | 233 | 3.29 | [2.05, 29.5] | 0.0063 → 0.0004 | not used: 27 independent events |

Game markets are, on this sample, calibrated (slope ≈ 1). The apparent
Sports miscalibration was a mixture artifact — exactly the kind of false
edge the gates exist to catch, and it was only visible once the cell keys
matched the market types. The Science and Technology cells have many rows
but few independent events (strike ladders on the same launch / release
date), so the event-count gate holds them out.

Consequence: until a refit on the 120-day pull produces qualifying cells
under the new keys, the bot reports "no usable cells" and does not trade.
That is the intended behaviour: no measured edge, no position.

## Refit on the 120-day pull (2026-09-08 21:10 UTC, 7,316 settled markets)

Zero per-category cells pass. The ones that matter:

| cell | n | events | slope | 95% CI | holdout Brier mkt → cal | blocked by |
|---|---|---|---|---|---|---|
| Sports/recurring\|24 | 1274 | ok | 0.97 | [0.86, 1.21] | 0.1818 → 0.1817 | slope CI includes 1 |
| Sports/recurring\|6 | 1434 | ok | 0.97 | [0.88, 1.24] | 0.1739 → 0.1747 | no improvement |
| Sports/recurring\|72 | 749 | ok | 0.92 | [0.72, 1.23] | 0.2076 → 0.2014 | slope CI includes 1 |
| Sports/recurring\|1 | 514 | ok | 1.02 | [0.67, 1.25] | 0.0877 → 0.0891 | no improvement |
| Climate and Weather/recurring\|24 | 1281 | ok | 1.21 | [0.97, 1.47] | 0.0400 → 0.0399 | slope CI includes 1 |
| Science and Technology/one_off\|24 | 566 | 65 | 1.72 | [1.29, 3.19] | 0.0183 → 0.0143 | < 100 independent events |
| Science and Technology/one_off\|72 | 572 | 66 | 1.47 | [1.04, 1.93] | 0.0289 → 0.0245 | < 100 independent events |
| Economics/recurring\|24 | 417 | 40 | 1.78 | [1.34, 3.02] | 0.0675 → 0.0639 | < 100 independent events |
| Commodities/one_off\|24 | 302 | 16 | 2.81 | [2.14, 22.7] | 0.0886 → 0.0696 | < 100 independent events |

Reading: with four-digit samples, Kalshi game markets (MLB, CS2, NBA, NPB…)
show no miscalibration at any horizon — the v6.1 `Sports|24` slope of
0.735 was entirely the futures/awards mixture. Weather daily highs likewise.
The categories that *look* miscalibrated (market underconfident, slope > 1)
are all strike ladders — dozens of markets per launch date, CPI print or oil
price — where the rows are not independent and the event-count gate holds.
Those cells could reach the gate with a longer history (≈180 days); that is
a pre-declared change to the sample, not to the gates, so the weekly Action
now pulls 180 days / 80 series per category. Pooled `ALL|h` cells pass at
three horizons but are never traded (categories are miscalibrated in
opposite directions).

Deployed consequence: `models/calibration.json` in this PR carries the
truthful state — 0 usable cells — so the bot idles on the generic sweep and
the Telegram boot message says "no usable cells". This replaces three cells
that would have traded a false edge.

## Next

1. Weekly Action refits on 180 days / 80 series per category; if a strike-
   ladder cell clears the event gate on two consecutive refits, discovery
   targets it automatically — watch `/state.json → model.discovery.per_cell`
   and the dashboard Scan-stage line.
2. The calibration edge on game markets is rejected: do not revisit without a
   new hypothesis (e.g. in-game state, which K1 already tested and closed).
3. The remaining structural edge is maker execution (spread capture) alone,
   which needs its own entry rule and pre-registration (K3: post-only
   two-sided quoting on high-volume recurring markets, sized to the
   inventory limit, evaluated on realised spread minus adverse selection).
