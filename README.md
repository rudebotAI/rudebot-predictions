# Prediction Market Quant Bot — Kalshi

Scans **Kalshi** prediction markets for +EV opportunities using a quant stack:

- **EV Gap Detection** — finds mispriced markets
- **Bayesian Updates** — adjusts probabilities from volume / momentum signals
- **LMSR Price Impact** — flags thin liquidity pools before sizing
- **KL-Divergence** — cross-event consistency check
- **Cross-event Arbitrage** — same-outcome priced differently across related markets
- **Kelly Criterion** — optimal position sizing (¼-Kelly by default)
- **Resolution Sniper** — late-cycle resolution gap trades
- **Order-Book Imbalance** — short-horizon directional bias

Runs 24/7 in **paper mode** by default. Sends Telegram alerts with Confirm /
Skip buttons before any trade.

> Polymarket support was removed (the bot is now Kalshi-only). The
> `connectors/polymarket.py` file and Polymarket env vars are gone from
> `main`. The earlier multi-platform scanners are kept as engines for
> cross-event arbitrage on Kalshi itself.

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
| `KALSHI_EMAIL` | yes | Kalshi account email |
| `KALSHI_API_KEY` | yes | from Kalshi → Account → API |
| `TELEGRAM_BOT_TOKEN` | recommended | for alerts |
| `TELEGRAM_CHAT_ID` | recommended | your chat id |
| `BRAVE_API_KEY` | optional | news-sentinel signal |
| `X_BEARER_TOKEN` | optional | news-sentinel signal |
| `COINBASE_API_KEY` / `COINBASE_API_SECRET` | optional | crypto research feed |

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

1. **Risk check** — stops if daily loss limit hit or too many consecutive losses
2. **Fetch markets** from Kalshi (top 50 by volume)
3. **EV scan** — estimate true probability, find gaps > 5%
4. **LMSR analysis** — estimate liquidity, flag thin pools
5. **Cross-event arbitrage** — flag same-outcome inconsistencies
6. **KL-divergence** — flag suspicious probability divergences
7. **Kelly sizing** — compute optimal bet size (capped at ¼-Kelly)
8. **Telegram alert** — send opportunity with Confirm / Skip buttons
9. **Exit check** — monitor open positions for take-profit / stop-loss

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
| Telegram confirm | `require_confirm: true` | Manual approval per trade |
| ¼-Kelly | `kelly_fraction: 0.25` | Conservative sizing |
| Max position | `$10` | Per-trade cap |
| Daily loss limit | `$20` | Auto-stops bot |
| Max consecutive losses | `3` | Pauses after losing streak |
| Max open positions | `5` | Prevents overexposure |
| Cooldown | `300s` | Pause after circuit breaker trips |

---

## Going live

**Only after paper results show consistent edge over 2-4+ weeks:**

1. Fund Kalshi account.
2. Set `BOT_MODE=live` and `KALSHI_API_KEY` / `KALSHI_EMAIL`.
3. Keep `require_confirm: true` initially.
4. Start with minimum sizes ($1-2 per trade).
5. Monitor via Telegram for at least a week before raising sizes.

> ⚠️ Real money = real risk. No guarantees of profit.

---

## Troubleshooting

- **"Max positions: 5/5" forever** — stale paper positions in the state store.
  Run `python tools/clear_positions.py --dry-run` then `--confirm` to flush.
- **No Telegram alerts** — check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **"Risk: ..." warnings** — circuit breaker tripped; `/resume` via Telegram.
- **Empty market scans** — Kalshi rate limit; increase scan interval.
- **Import errors** — `pip install -r requirements.txt`.

---

## Deployment

This repo deploys on Railway out of the box:

- `railway.toml` defines the start command (`python main.py`) and restart policy.
- Set the env vars above in the Railway → Variables panel.
- Mount a Railway volume at `/app/state` so `state.json` (positions, PnL,
  IV history) survives deploys.
