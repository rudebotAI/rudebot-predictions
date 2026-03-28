# Prediction Market Quant Bot

Scans Polymarket + Kalshi for +EV opportunities using a full quant stack:
- **EV Gap Detection** -- finds mispriced markets
- **Kelly Criterion** -- optimal position sizing (quarter-Kelly for safety)
- **LMSR Price Impact** -- flags thin liquidity pools
- **KL-Divergence** -- cross-platform price discrepancy scanner
- **Bayesian Updates** -- adjusts probabilities from volume/momentum signals
- **Cross-Platform Arbitrage** -- detects same-event price gaps

Runs 24/7 in **paper mode** by default. Sends Telegram alerts with confirm/skip buttons before any trade.

---

## Quick Start

### 1. Install dependencies
```bash
cd prediction-bot
pip install -r requirements.txt
```

### 2. Configure
```bash
cp config.yaml config.yaml.backup   # optional
```

Edit `config.yaml`:
- **Telegram** (required for alerts): Set `bot_token` and `chat_id`
- **Polymarket** (optional): Set `private_key` for authenticated access
- **Kalshi** (optional): Set `email` and `api_key`
- Leave `mode: paper` -- do NOT change to `live` until you've validated performance

#### Getting a Telegram Bot Token
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow prompts
3. Copy the token into `config.yaml`
4. Send any message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`

### 3. Run
```bash
# Normal mode -- scans every 120s until stopped
python main.py

# Single scan -- run once and exit (good for testing)
python main.py --once
```

### 4. Telegram Commands
Once running, send these to your bot:
- `/pnl` -- Current performance summary
- `/status` -- Risk manager state
- `/positions` -- Open paper positions
- `/resume` -- Reset risk manager after circuit breaker
- `/help` -- List commands

---

## How It Works

Each scan cycle:
1. **Risk check** -- stops if daily loss limit hit or too many consecutive losses
2. **Fetch markets** from Polymarket + Kalshi (top 50 each)
3. **Cross-reference** -- match same events across platforms
4. **EV scan** -- estimate true probability, find gaps > 5%
5. **LMSR analysis** -- estimate liquidity, flag thin pools
6. **Arbitrage scan** -- find same-event price gaps across platforms
7. **KL-Divergence** -- flag significant cross-platform probability divergences
8. **Kelly sizing** -- compute optimal bet size (capped at quarter-Kelly)
9. **Telegram alert** -- send opportunity with Confirm/Skip buttons
10. **Exit check** -- monitor open positions for take-profit/stop-loss

---

## Project Structure

```
prediction-bot/
├── main.py                  # Entry point + orchestrator
├── config.yaml              # All settings (API keys, risk params)
├── requirements.txt
├── connectors/
│   ├── polymarket.py        # Polymarket CLOB API wrapper
│   └── kalshi.py            # Kalshi REST API wrapper
├── engines/
│   ├── scanner.py           # EV gap detector
│   ├── sizing.py            # Kelly criterion sizing
│   ├── lmsr.py              # LMSR price impact calculator
│   ├── divergence.py        # KL-divergence scanner
│   ├── bayesian.py          # Bayesian probability updater
│   └── arbitrage.py         # Cross-platform arb detector
├── execution/
│   ├── paper.py             # Paper trading engine
│   ├── live.py              # Live execution (Phase 2)
│   └── risk.py              # Risk manager + circuit breakers
├── alerts/
│   └── telegram.py          # Telegram alerts + inline buttons
└── logs/                    # Auto-created at runtime
    ├── trades.json
    └── performance.json
```

---

## Safety Features

| Feature | Default | Purpose |
|---------|---------|---------|
| Paper mode | `mode: paper` | No real money until you flip it |
| Telegram confirm | `require_confirm: true` | Must approve every trade |
| Quarter-Kelly | `kelly_fraction: 0.25` | Conservative sizing |
| Max position | `$10` | Per-trade cap during testing |
| Daily loss limit | `$20` | Auto-stops bot |
| Max consecutive losses | `3` | Pauses after losing streak |
| Max open positions | `5` | Prevents overexposure |
| Cooldown | `300s` | Pause after circuit breaker trips |

---

## Going Live (Phase 2)

**Only after paper results show consistent edge:**

1. Fund Polymarket wallet (Polygon USDC) and/or Kalshi account
2. Add credentials to `config.yaml`
3. Change `mode: live`
4. Keep `require_confirm: true` initially
5. Start with minimum sizes ($1-2 per trade)
6. Monitor via Telegram for at least a week before increasing size

⚠️ **Real money = real risk.** The bot makes no guarantees of profit.

---

## Troubleshooting

- **"Config not found"** -- Copy `config.yaml` and fill in your keys
- **No Telegram alerts** -- Check `bot_token` and `chat_id` are correct
- **"Risk: ..." warnings** -- Bot paused due to circuit breaker; send `/resume` via Telegram
- **Empty market scans** -- API rate limits; increase `scan_interval_sec`
- **Import errors** -- Run `pip install -r requirements.txt`
