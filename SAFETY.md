# SAFETY — rudebot-predictions

This document enumerates every safety gate in the bot. Before you flip any
live-trading flag, read this end-to-end. Real money is at stake.

## Non-negotiables

1. **`mode: paper` is the default** and must stay that way in
   `config.yaml.example`. Changing this default is a breaking change.
2. **`require_confirm: true`** — every live trade must be approved via
   Telegram confirm/skip buttons. Paper mode auto-fills at the touch.
3. **Per-trade cap** — `max_position_usd` (default $10) is enforced by the
   risk manager. New strategies must respect it.
4. **Daily loss limit** — `max_daily_loss_usd` (default $20) auto-stops
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
| engines/resolution_sniper.py | `resolution_sniper.enabled` | `false` | not wired into main loop |
| engines/obi.py | `obi.enabled` | `false` | not wired into main loop |
| engines/market_maker.py | `market_maker.enabled` | `false` | not wired into main loop |
| execution/orders.py, order_router.py, state_store.py, risk.py | n/a | — | legacy primitives, not on the live path |

## Live execution guards (v6, `execution/live.py`)

Every live order goes through, in order:

1. **Fresh quote** — re-fetch the market; never trade on a stale scan price.
2. **Spread cap** — refuse if `yes_ask - yes_bid > max_spread` (default 5¢).
3. **Depth** — size is clipped to the resting size at the touch
   (`yes_ask_size_fp` / `yes_bid_size_fp`).
4. **Price grid** — the limit price is snapped onto the market's
   `price_ranges` (sub-penny structures exist); buys round down, sells up.
5. **Risk manager** — `can_trade()` at the actual notional.
6. **IOC only** — orders are `immediate_or_cancel`; nothing rests unwatched.
   Only the exchange-reported `fill_count` at `average_fill_price` is booked.
   Zero fill = no position, nothing recorded.
7. **Exits are reduce-only** IOC sells; partial exits are booked as partial.

## Idempotency

Each order carries a deterministic `client_order_id`
(`rb-` + sha256(market, side, action, scan nonce)). Order creates are never
auto-retried on 429/timeout — a create that may have succeeded is reconciled
by `get_order()` rather than resent.

## Circuit Breakers

- **Daily loss** — halts when realized losses reach `max_daily_loss_usd`; clears at UTC midnight.
- **Consecutive losses** — halts after N losses; auto-resumes after `cooldown_seconds` *and resets the streak* (previously it re-tripped immediately).
- **Max drawdown (sticky)** — equity below `(1 - max_drawdown_pct)` × peak (default 8%) halts until a human sends `/resume`, which re-bases the peak.
- **Per-event exposure** — legs of one event share `max_event_exposure_usd`.
- **Max open positions / per-position cap / duplicate market** — as before.

All of these fire because closures now call `record_exit()`; in v5 nothing
did, so the daily-loss and streak breakers could never trip and the
position count only ever grew.

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
