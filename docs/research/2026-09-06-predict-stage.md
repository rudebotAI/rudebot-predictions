# Predict stage — where a $10-position Kalshi bot can credibly find edge

Date: 2026-09-06. Sources at the end. Companion to the v6.0 audit (execution/risk
were rebuilt there; this document is about *what the bot predicts* and *how it
is executed*, the two things the audit flagged as still heuristic).

## 1. Fees gate everything

Kalshi's July 7 2026 fee schedule: taker fee = 0.07 · C · P · (1−P) per contract
(≈1.75¢ at P=0.50, ≈0.33¢ at P=0.95). A maker formula exists (0.0175·C·P·(1−P))
but its multiplier is 0 in nearly every series, so **resting orders are free**.
The retail Volume Incentive Program pays ≤ $0.005/contract pro-rata; the real
liquidity subsidies are MM-agreement only.

## 2. Ranked edges (evidence, not vendor marketing)

| # | Edge | Evidence | Net edge est. | Verdict |
|---|---|---|---|---|
| 1 | **Post only, never take** | Bürgi–Deng–Whelan "Makers and Takers" (46,282 contracts 2021–25): makers −12% vs takers −31% avg return; mechanically 0 fee + spread capture | ~1–2 pp per round trip vs taking the same trade | **credible** (structural, not alpha) |
| 2 | Category × horizon mis-calibration | Le (2026), 64.7M trades: political prices compressed toward 50% (slopes 0.93–1.83 by horizon), weather too extreme at short horizons (0.69–0.97), sports near-calibrated mid-life; Whelan et al.: 70–99¢ contracts earn small positive pre-fee returns | 0–3 pp, category-specific, sign differs by category | **plausible, measurable from free data** |
| 3 | Sports final-10-minutes insurance demand | "Prices, Probabilities, and Parlays" (23M NBA/MLB/NHL trades, 2026): calibration deteriorates sharply in the last 10 min (losing holders buy insurance, leader underpriced) | guess 1–3 pp, unquantified | plausible-unproven; execution-heavy |
| 4 | Weather vs NWS/NBM | No peer-reviewed profitability; retail postmortems negative (0/32 buying cheap tails); Kalshi settles daily markets on the NWS climate report, hourly on its own index | unknown, negative for naive models | plausible-unproven |
| 5 | Cross-venue vs Polymarket | "Semantic Non-Fungibility": ~6% of Kalshi events have a twin; 2–4% persistent gaps from rule/netting differences | 0–1 pp one-sided | signal only |
| 6 | Macro data vs consensus | Fed Board WP 2026-010: Kalshi CPI median beats Bloomberg consensus; no systematic mispricing | ~0 | noise |

## 3. What v6.1 builds from this

**Execute (edge #1, credible).** Every entry is a post-only GTC limit at our
side's bid with an exchange-side expiry (`entry_style: maker`,
`rest_seconds: 1800`). Only exchange-reported fills are booked. Paper mode
simulates the same order with a *conservative* fill rule: filled only when a
later poll shows the market traded through our price. Exits stay reduce-only IOC.

**Predict (edge #2, measurable).** The legacy heuristic model is OFF
(`allow_heuristic: false`). The only probability source is a recalibration
map estimated from Kalshi's own settled markets via the free public API:

    logit P(yes | price p, category c, horizon h) = a_{c,h} + b_{c,h} · logit(p)

fitted per (category, horizon ∈ {1, 6, 24, 72, 168}h) with a chronological
70/30 holdout, a bootstrap CI on the slope, and reliability tables. A cell is
used only when (i) n ≥ 150, (ii) the holdout Brier score improves over the
market's own, and (iii) the slope CI excludes 1. No pooled fallback — a weather
market never borrows a politics slope. Without a usable cell the model returns
the market price, i.e. **no edge, no trade**.

Edge = calibrated P − our resting price, gated at 0.04 (fee-adjusted EV gate
unchanged). Kelly sizing unchanged.

**Scoring.** Every resolved position appends (model_prob, entry price, outcome)
to `logs/calibration.jsonl`; the dashboard shows the model's Brier score next
to the market's. If the model does not beat the market on ≥ 100 resolved
positions, the calibration cells are re-fitted or the bot idles — P&L alone is
not the verdict.

## 4. Pre-registered candidates (not yet run)

- **K1 — sports final-minutes leader.** For NBA/MLB/NHL moneylines in the last
  10 minutes, rest a bid on the leading side when the calibrated probability
  exceeds price by ≥ 0.04. Backtest first from `GET /markets/trades` on settled
  games. Gates: positive net return in ≥ 3 of 4 monthly slices; Brier
  improvement on holdout; fill-rate assumption ≤ observed maker fill rate.
- **K2 — weather short-horizon fade.** Only if the `Climate and Weather|1h`
  / `6h` cells are usable with slope < 1 and the Brier improvement survives a
  second month of data.

Hard rules: one hypothesis at a time; every fit is logged with its date and
sample; no parameter is tuned after a cell is deployed.

## 4b. W1 result (2026-09-07, first fit — recorded before any trade)

Dataset: 3,225 settled markets, 60 days, ≤30 series × ≤30 markets per category
(`research/cache/settled_20260906.jsonl`). 47 (category, horizon) cells fitted.
**Usable cells: 0.** The bot therefore idles (scans, finds no edge, trades
nothing) until a refit unlocks a cell. What the fit says:

| Cell | n | events | slope | holdout Brier mkt → cal | verdict |
|---|---|---|---|---|---|
| Sports 6h / 24h | 664 / 650 | 294 / 291 | 1.11 / 0.98 | 0.182→0.187 / 0.208→0.209 | calibrated — no edge (matches literature) |
| Climate and Weather 24h | 493 | 82 | 1.24 | 0.054→0.055 | no OOS gain; too few events |
| Commodities 1h | 544 | 31 | 1.99 | 0.057→0.050 | passes Brier + CI but 544 samples come from 31 strike ladders — not independent; rejected by the events gate (added after seeing this) |
| Politics / Elections | 48 / 37 | — | — | — | too few settled markets in 60 days |

Lessons folded into the code: horizons measured from `expected_expiration_time`
(close_time is rewritten on early close); event-grouped holdout and bootstrap;
slope/intercept bounds; ≥ 100 independent events per cell. The weekly refit
pulls 120 days and more series per category to give the slow categories a
chance. Multiplicity: 47 cells are tested every refit — a cell must pass in
two consecutive refits before it is trusted for live money (go-live rule).

## 5. Weekly plan

- W1 (now): model v1 from 60 days of settled markets; maker execution; paper.
- W2: re-fit on 90 days; compare cells; publish reliability tables to the
  dashboard; start K1 backtest data pull.
- W3–4: paper record scored on Brier + fill rate; K1 verdict.
- W5+: go-live case only if the model beats the market on ≥ 100 resolved
  positions AND paper P&L is positive after 4 weeks AND maker fill rate ≥ 25%.

## Sources

Bürgi, Deng & Whelan, *Makers and Takers* (karlwhelan.com/Papers/Kalshi.pdf; GWU WP 2026-001; CEPR VoxEU column) · Le (2026), arXiv 2602.19520 · *Prices, Probabilities, and Parlays*, arXiv 2607.14430 · *Semantic Non-Fungibility*, arXiv 2601.01706 · Diercks, Katz & Wright, *Kalshi and the Rise of Macro Markets*, FEDS 2026-010 · Kalshi fee schedule (kalshi.com/docs/kalshi-fee-schedule.pdf, 2026-07-07) · Kalshi Volume Incentive Program & Liquidity Provider Program help articles · Kalshi API changelog (docs.kalshi.com/changelog) · Northlake Labs weather postmortem.
