# Research stage

Free, unauthenticated data from Kalshi's public API drives the Predict stage.

    PYTHONPATH=. python research/kalshi_history.py --days 60 --per-series 30 --series-per-category 30
    PYTHONPATH=. python research/calibration.py research/cache/settled_<date>.jsonl

`kalshi_history.py` pulls settled markets per series (with category), hourly
candlesticks, and the mid price at 1/6/24/72/168 hours before the SCHEDULED
resolution (`expected_expiration_time`; `close_time` is rewritten on early
close and must not be used), skipping any horizon at which the market was no
longer trading. `calibration.py` fits one logistic recalibration per
(category, horizon) with an event-grouped chronological holdout and an event
bootstrap, and writes `models/calibration.json`. Only cells that pass every
gate (n >= 150, >= 50 mid-range prices, slope in [0.5, 3], holdout Brier
improvement, slope CI excluding 1) are marked `used`; the bot trades nothing
else. `.github/workflows/refit-calibration.yml` re-runs this weekly and
commits the model.

Method and evidence: `docs/research/2026-09-06-predict-stage.md`.
