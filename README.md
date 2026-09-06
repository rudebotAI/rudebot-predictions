# Prediction Market Quant Bot — Kalshi (v6.0)

Scans **Kalshi** prediction markets for +EV opportunities using a quant stack:

- **EV Gap Detection** — finds mispriced markets
- **Bayesian Updates** — adjusts probabilities from volume / momentum signals
- **LMSR Price Impact** — flags thin liquidity pools before sizing
- **KL-Divergence** — cross-event consistency check
- **Cross-event Arbitrage** — same-outcome priced differently across related markets
- **Kelly Criterion** — fractional Kelly on the live bankroll (¼-Kelly by default), capped per position / per event
- **Risk stack** — daily-loss, loss-streak, and **8% max-drawdown** circuit breakers; per-trade Sharpe tracked
- **Resolution Sniper** — late-cycle resolution gap trades
- **Order-Book Imbalance** — short-horizon directional bias

Runs 24/7 in **paper mode** by default. Sends Telegram alerts with Confirm /
Skip buttons before any trade.

> **v6.0 (2026-09-06)** — migrated to Kalshi's **V2 order API**
> (`/portfolio/events/orders`; the legacy `/portfolio/orders` mutations were
> deprecated in June 2026), fixed-point dollar prices with per-market tick
> grids, IOC execution with fill verification, and the 6-stage risk
> framework. See `SAFETY.md` and the changelog at the bottom.
>
> Polymarket is read-only reference data for cross-venue checks; execution is Kalshi-only.

---

## Quick start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in at least:

| Var | Required? | Notes |
|---|---|---|
| `BOT_MODE` | yes | `paper` or `live`. Start with `paper`. |
| `KALSHI_ACCESS_KEY` | live only | API key ID from Kalshi → Account → API Keys |
| `KALSHI_PRIVATE_KEY` | live only | RSA private key PEM for that key (one-line with `\n` is fine) |
| `TELEGRAM_BOT_TOKEN` | live: required | alerts + per-trade Confirm/Skip buttons |
| `TELEGRAM_CHAT_ID` | live: required | your chat id |

Paper mode needs **no credentials** — Kalshi market data is public. Risk
parameters live in `config.yaml` (every `RiskConfig` field is tunable there
or via env, see `.env.example`).

### Getting a Telegram Bot Token

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`.
2. Copy the token.
3. Send any message to your bot, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.

### 3. Run

```bash
# Normal mode — scans every 120s until stopped
python main.py

# Single scan — run once and exit (good for testing)
python main.py --once
```

### 4. Telegram commands (once running)

- `/pnl` — current performance summary
- `/status` — risk-manager state
- `/positions` — open positions
- `/resume` — reset risk manager after circuit breaker
- `/help` — list commands

---

## How a scan cycle works

1. **Closures** — mark open positions (own-leg prices), resolve settled
   markets, stop-loss / take-profit; realized P&L feeds the risk manager
2. **Telegram** — execute confirmed trades, answer `/pnl` `/status` `/positions` `/resume`
3. **Scan** — Kalshi `/events` → per-event `/markets` (≤6 per event, by 24h volume)
4. **Predict** — model probability, fee-adjusted EV; gate on **edge ≥ 0.04** and EV ≥ 0.05
5. **Size** — fractional Kelly on the bankroll (paper: `bankroll_usd` + P&L; live: Kalshi balance),
   capped by `max_position_usd` and `max_portfolio_pct`
6. **Risk** — halts, drawdown, daily budget, per-event exposure, duplicates
7. **Execute** — paper fills at the touch; live sends a Telegram confirm, then an
   **IOC limit at ask + slippage** via the V2 API, booking only what filled

---

## Project structure

```
.
├── main.py                # Entry point + orchestrator
├── env_config.py          # Env → typed config
├── risk_manager.py        # Risk caps, circuit breakers, /status, /resume
├── connectors/
│   └── kalshi.py          # Kalshi REST + per-event enrichment
├── engines/
│   ├── scanner.py         # EV gap detector
│   ├── sizing.py          # Kelly criterion
│   ├── lmsr.py            # LMSR price impact
│   ├── divergence.py      # KL-divergence
│   ├── bayesian.py        # Bayesian probability updates
│   ├── arbitrage.py       # Cross-event arb
│   ├── obi.py             # Order-book imbalance
│   ├── resolution_sniper.py
│   ├── market_maker.py
│   ├── fair_value.py
│   └── auto_redeem.py
├── execution/
│   ├── paper.py           # Paper trading engine
│   ├── live.py            # Live execution
│   ├── orders.py
│   ├── order_router.py
│   ├── risk.py
│   └── state_store.py     # Position persistence
├── subbots/
│   ├── news_sentinel.py
│   └── price_tracker.py
├── alerts/
│   └── telegram.py
├── tools/
│   └── clear_positions.py # Manual paper-state flush
├── tests/
└── logs/
```

---

## Safety features

| Feature | Default | Purpose |
|---|---|---|
| Paper mode | `BOT_MODE=paper` | No real money until you flip it |
| Telegram confirm | `require_confirm: true` | Manual approval per live trade |
| Edge gate | `min_edge_bps: 400` | Only trade ≥4pt probability edge after fees |
| ¼-Kelly | `kelly_fraction: 0.25` | Conservative sizing on the real bankroll |
| Max position | `$10` / 5% of bankroll | Per-trade cap |
| Per-event exposure | `$20` | Correlated legs share a budget |
| Daily loss limit | `$20` | Auto-stops bot |
| Max drawdown | `8%` | Sticky halt until `/resume` |
| Max consecutive losses | `3` | Pauses after losing streak (cooldown resets streak) |
| Max open positions | `5` | Prevents overexposure |
| Spread / slippage | `5¢` / `1¢` | Live orders refuse wide books, cross by at most 1¢ |
| IOC execution | always | No unwatched resting orders; only fills are booked |

---

## Going live

**Only after paper results show consistent edge over 2-4+ weeks:**

1. Fund Kalshi account.
2. Set `BOT_MODE=live`, `KALSHI_ACCESS_KEY`, `KALSHI_PRIVATE_KEY`.
3. Keep `require_confirm: true` initially.
4. Start with minimum sizes ($1-2 per trade).
5. Monitor via Telegram for at least a week before raising sizes.

> ⚠️ Real money = real risk. No guarantees of profit.

---

## Troubleshooting

- **"Max positions: 5/5" forever** — stale positions in `logs/risk_state.json`
  (v5 never released them). Run `python tools/clear_positions.py --dry-run`
  then `--confirm` to flush, or delete `logs/risk_state.json` once.
- **"HALTED: Max drawdown …"** — the 8% breaker is sticky by design; review,
  then send `/resume` on Telegram.
- **No Telegram alerts** — check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **"Risk: ..." warnings** — circuit breaker tripped; `/resume` via Telegram.
- **Empty market scans** — Kalshi rate limit; increase scan interval.
- **Import errors** — `pip install -r requirements.txt`.

---

## Deployment

This repo deploys on Railway out of the box:

- `railway.toml` defines the start command (`python main.py`) and restart policy.
- Set the env vars above in the Railway → Variables panel.
- Mount a Railway volume at `/data` — `main.py` symlinks `logs/` there so
  `trades.json`, `live_trades.json` and `risk_state.json` survive deploys.
- Python is pinned to 3.12 (`railway.toml`, `Dockerfile`, CI).

---

## Changelog

### v6.0 — 2026-09-06
- **Kalshi V2 orders**: `POST /portfolio/events/orders` with `bid`/`ask`
  book side, fixed-point `price`/`count`, `time_in_force`,
  `self_trade_prevention_type`; V2 cancel; `get_order()` status polling;
  `balance_dollars`; `position_fp`. Legacy `/log-in` removed.
- **Prices in dollars end-to-end**, snapped to each market's `price_ranges`
  (sub-penny tick structures). No more `int(price*100)`.
- **Execution**: fresh quote, spread cap, depth clip, IOC at ask+slippage,
  book only the filled quantity at the average fill; reduce-only IOC exits;
  live position ledger (`logs/live_trades.json`); deterministic client IDs.
- **Risk**: `record_exit()` wired (breakers actually fire); cooldown resets
  the loss streak; 8% max-drawdown sticky breaker; per-event exposure cap;
  per-trade returns + Sharpe; atomic state writes.
- **Sizing**: `KellySizer` wired with a real bankroll; edge gate from
  config (`min_edge_bps`, default 400 = 0.04) instead of a hardcoded 0.005.
- **P&L bug**: NO-side P&L was inverted (mixed NO entry with YES exit).
  All prices are now in the position's own leg.
- **Telegram**: `/pnl` `/status` `/positions` `/resume` work in both modes.
- **Ops**: `--once` flag; dashboard escapes market titles; per-event scan cap;
  Python 3.12; CI compiles + import-checks and validates the shipped
  `config.yaml`; four unparseable dead modules removed.
