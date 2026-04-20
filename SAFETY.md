# SAFETY — rudebot-predictions

This document enumerates every safety gate in the bot. Before you flip any
live-trading flag, read this end-to-end. Real money is at stake.

## Non-negotiables

1. **`mode: paper` is the default** and must stay that way in
   `config.yaml.example`. Changing this default is a breaking change.
2. **`require_confirm: true`** — every trade must be approved via Telegram
   confirm/skip buttons. This applies to both paper and live modes.
3. **Per-trade cap** — `max_position_usd` (default $10) is enforced by the
   risk manager. New strategies must respect it.
4. **Daily loss limit** — `daily_loss_limit_usd` (default $20) auto-stops
   the bot. No strategy is allowed to bypass this.
5. **Max consecutive losses** — bot pauses after N losses in a row.
6. **No module adds live-execution code paths that can be hit without
   explicit opt-in via config.** See the "Opt-in gates" section.

## Opt-in gates (new modules)

Each new engine ships with `enabled: False` in its DEFAULTS dict. To enable
a strategy you must:

1. Edit `config.yaml` to set `<strategy>.enabled: true`.
2. Run for at least one session in paper mode with live market data.
3. Review the logs and verify signal quality BEFORE flipping `mode: live`.

| Module | Config key | Default | Live-safe? |
|---|---|---|---|
| engines/copy_trader.py | `copy_trading.enabled` | `false` | gated |
| engines/polygon_whale.py | `whale.enabled` | `false` | gated |
| engines/resolution_sniper.py | `resolution_sniper.enabled` | `false` | gated |
| engines/obi.py | `obi.enabled` | `false` | gated |
| engines/market_maker.py | `market_maker.enabled` | `false` | gated |
| connectors/limitless.py | `limitless.enabled` | `false` | read-only; `place_order` is `NotImplementedError` |
| execution/orders.py | n/a (primitives) | — | passive; depth guard runs on every order |

## Depth Guard

`execution/orders.py::check_depth` runs before every order placement and
raises `DepthGuardError` if:

- the taker-side book doesn't have `min_depth_usd` ($200 default) of resting
  size within `max_price_slippage` (1¢ default) of the intent price; or
- the order size exceeds the available depth within tolerance.

A `DepthGuardError` aborts the trade and does not retry.

## Idempotency

Every order intent carries a `client_order_id` derived from
`(source, market_id, salt, 5-second epoch bucket)`. Venues that honor
client order IDs will reject duplicates on retry.

## Circuit Breakers (existing)

- Daily PnL circuit breaker (auto-stop on loss limit)
- Consecutive losses circuit breaker (pause after N losses)
- Cooldown window after circuit breaker trips (default 300s)

## Adding a new strategy safely

1. Add the module under `engines/`.
2. Define a `DEFAULTS` dict with `"enabled": False`.
3. Emit trade *candidates* as dicts — do not call execution directly.
4. Route candidates through the Telegram confirm path in `alerts/telegram.py`.
5. Document the module's risk profile at the top of the file.
6. Add unit tests under `tests/` covering the guard rails, not just the
   happy path.

## Live-trading checklist

Before `mode: live`:

- [ ] Paper mode has run for >= 7 calendar days on live market data.
- [ ] Paper PnL is positive after fees (not just gross).
- [ ] Depth-guard rejection rate is logged and looks sane.
- [ ] No circuit-breaker trips in the past 48 hours.
- [ ] `require_confirm: true` is set and Telegram chat is monitored.
- [ ] Initial position sizes are set to the floor ($1–2/trade).
- [ ] A kill switch is documented and tested.

## Performance budgets (advisory)

| Stage | Budget |
|---|---|
| Scanner scan cycle | < 2s |
| Candidate construction | < 100ms |
| Depth guard check | < 50ms |
| Telegram alert round-trip | < 5s |
| Paper fill simulation | < 10ms |

Violations are not errors but should be logged at WARNING.
