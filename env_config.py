"""
Environment-aware configuration loader for PredictionBot.
Loads secrets from environment variables (or .env file),
strategy/risk params from config.yaml.

v6 — Secrets never touch config files; all RiskConfig fields are YAML/env tunable.
"""

import os
import yaml
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — Railway/Docker set env vars directly


@dataclass
class KalshiSecrets:
    email: str = ""
    api_key: str = ""
    # Live trading auth (RSA-PSS request signing). access_key is the API Key ID;
    # private_key is the RSA private key in PEM format. Both required for live.
    access_key: str = ""
    private_key: str = ""


@dataclass
class PolymarketSecrets:
    private_key: str = ""


@dataclass
class TelegramSecrets:
    bot_token: str = ""
    chat_id: str = ""
    # Every live trade must be confirmed via Telegram. Keep True for safety.
    require_confirm: bool = True


@dataclass
class RiskConfig:
    # Hard caps (defaults match README/SAFETY.md -- small until paper proves edge)
    max_daily_loss_usd: float = 20.0
    max_position_usd: float = 10.0
    max_open_positions: int = 5
    max_consecutive_losses: int = 3
    cooldown_seconds: int = 300
    # Sizing: fractional Kelly on the bankroll, capped by max_position_usd
    kelly_fraction: float = 0.25
    bankroll_usd: float = 500.0          # paper bankroll; live uses Kalshi balance
    max_portfolio_pct: float = 0.05      # single position <= 5% of bankroll
    max_event_exposure_usd: float = 20.0 # correlated legs of one event share this
    # Edge gates (6-stage framework: edge threshold 0.04)
    min_ev_threshold: float = 0.05
    min_edge_bps: int = 400              # 0.04 probability edge after fees
    max_days_to_resolution: int = 90
    # Drawdown circuit breaker (sticky halt; /resume to clear)
    max_drawdown_pct: float = 0.08
    # Live execution guards (yes-leg dollars)
    max_spread: float = 0.05
    slippage: float = 0.01
    # Exit discipline on marked positions (fraction of entry)
    stop_loss_pct: float = -0.50
    take_profit_pct: float = 1.00

    @property
    def min_edge(self) -> float:
        return self.min_edge_bps / 10000.0


@dataclass
class BotConfig:
    mode: str = "paper"
    scan_interval: int = 120
    auto_trade: bool = False
    platforms: list = field(default_factory=lambda: ["kalshi"])
    kalshi: KalshiSecrets = field(default_factory=KalshiSecrets)
    polymarket: PolymarketSecrets = field(default_factory=PolymarketSecrets)
    telegram: TelegramSecrets = field(default_factory=TelegramSecrets)
    risk: RiskConfig = field(default_factory=RiskConfig)
    research_sources: list = field(default_factory=lambda: ["brave", "x"])


def load_config(config_path: str = None) -> BotConfig:
    """
    Load configuration from YAML + environment variables.
    Env vars always override YAML for secrets.
    """
    # Resolve config path
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.yaml")

    cfg = BotConfig()

    # Load YAML for strategy/risk params (NOT secrets)
    yaml_path = Path(config_path)
    if yaml_path.exists():
        try:
            with open(yaml_path) as f:
                raw = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {yaml_path}")
            _apply_yaml(cfg, raw)
        except Exception as e:
            logger.warning(f"Failed to load {yaml_path}: {e}")
    else:
        logger.info(f"No config file at {yaml_path}, using defaults + env vars")

    # Override mode from env
    cfg.mode = os.getenv("BOT_MODE", cfg.mode)

    # Load ALL secrets from env vars (never from YAML)
    cfg.kalshi.email = os.getenv("KALSHI_EMAIL", "")
    cfg.kalshi.api_key = os.getenv("KALSHI_API_KEY", "")
    cfg.kalshi.access_key = os.getenv("KALSHI_ACCESS_KEY", "")
    # PEM private key; allow literal "\n" in the env var (Railway one-liners).
    _pk = os.getenv("KALSHI_PRIVATE_KEY", "")
    cfg.kalshi.private_key = _pk.replace("\\n", "\n") if _pk else ""
    cfg.polymarket.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    cfg.telegram.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    _rc = os.getenv("TELEGRAM_REQUIRE_CONFIRM")
    if _rc is not None:
        cfg.telegram.require_confirm = _rc.strip().lower() in {"1", "true", "yes", "on"}

    # Risk overrides from env
    if os.getenv("MAX_DAILY_LOSS_USD"):
        cfg.risk.max_daily_loss_usd = float(os.getenv("MAX_DAILY_LOSS_USD"))
    if os.getenv("MAX_POSITION_USD"):
        cfg.risk.max_position_usd = float(os.getenv("MAX_POSITION_USD"))
    if os.getenv("MAX_OPEN_POSITIONS"):
        cfg.risk.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS"))
    if os.getenv("KELLY_FRACTION"):
        cfg.risk.kelly_fraction = float(os.getenv("KELLY_FRACTION"))
    if os.getenv("MAX_DAYS_TO_RESOLUTION"):
        cfg.risk.max_days_to_resolution = int(os.getenv("MAX_DAYS_TO_RESOLUTION"))
    if os.getenv("BANKROLL_USD"):
        cfg.risk.bankroll_usd = float(os.getenv("BANKROLL_USD"))
    if os.getenv("MIN_EDGE_BPS"):
        cfg.risk.min_edge_bps = int(os.getenv("MIN_EDGE_BPS"))
    if os.getenv("MAX_DRAWDOWN_PCT"):
        cfg.risk.max_drawdown_pct = float(os.getenv("MAX_DRAWDOWN_PCT"))
    if os.getenv("MAX_EVENT_EXPOSURE_USD"):
        cfg.risk.max_event_exposure_usd = float(os.getenv("MAX_EVENT_EXPOSURE_USD"))

    # Validate critical config
    _validate(cfg)

    return cfg


def _apply_yaml(cfg: BotConfig, raw: dict):
    """Apply YAML values to config (strategy/risk params only)."""
    cfg.mode = raw.get("mode", cfg.mode)
    cfg.scan_interval = raw.get("scan_interval", cfg.scan_interval)
    cfg.auto_trade = raw.get("auto_trade", cfg.auto_trade)
    cfg.platforms = raw.get("platforms", cfg.platforms)
    cfg.research_sources = raw.get("research_sources", cfg.research_sources)

    # Risk params from YAML. Any RiskConfig field may appear under `risk:`;
    # `strategy:` is accepted as a legacy alias (older config.yaml.example).
    merged = {}
    for section in ("strategy", "risk"):
        block = raw.get(section) or {}
        if isinstance(block, dict):
            merged.update(block)
    if merged:
        for key in RiskConfig.__dataclass_fields__:
            if key in merged and merged[key] is not None:
                current = getattr(cfg.risk, key)
                try:
                    setattr(cfg.risk, key, type(current)(merged[key]))
                except (TypeError, ValueError):
                    logger.warning(f"CONFIG: ignoring risk.{key}={merged[key]!r} (bad type)")


def _validate(cfg: BotConfig):
    """Validate config and warn about missing values."""
    warnings = []

    if cfg.mode == "live":
        # Live trading requires RSA signing auth + Telegram confirm. main.py
        # enforces these as a hard gate (refuses to trade if unmet); these are
        # the human-readable warnings explaining why.
        if not cfg.kalshi.access_key:
            warnings.append("LIVE: KALSHI_ACCESS_KEY not set — live trading will be refused")
        if not cfg.kalshi.private_key:
            warnings.append("LIVE: KALSHI_PRIVATE_KEY not set — live trading will be refused")
        if not (cfg.telegram.bot_token and cfg.telegram.chat_id):
            warnings.append("LIVE: Telegram not fully configured — live trading will be refused")
        if not cfg.telegram.require_confirm:
            warnings.append("LIVE: require_confirm is False — live trading will be refused (confirm is mandatory)")
    if not cfg.telegram.bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN not set — alerts disabled")

    if cfg.risk.kelly_fraction > 0.5:
        warnings.append(f"Kelly fraction {cfg.risk.kelly_fraction} is aggressive (>0.5)")
    if cfg.risk.min_edge < 0.02:
        warnings.append(f"min_edge {cfg.risk.min_edge:.3f} is below Kalshi's fee floor at mid prices")
    if cfg.risk.max_drawdown_pct <= 0:
        warnings.append("max_drawdown_pct is 0 -- drawdown circuit breaker disabled")

    for w in warnings:
        logger.warning(f"CONFIG: {w}")
