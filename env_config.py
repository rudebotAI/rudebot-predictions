"""
Environment-aware configuration loader for PredictionBot.
Loads secrets from environment variables (or .env file),
strategy/risk params from config.yaml.

v4.1 — Secrets never touch config files.
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
    max_daily_loss_usd: float = 100.0
    max_position_usd: float = 50.0
    max_open_positions: int = 5
    max_consecutive_losses: int = 3
    kelly_fraction: float = 0.25
    cooldown_seconds: int = 300
    min_ev_threshold: float = 0.05
    min_edge_bps: int = 200
    max_days_to_resolution: int = 90


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

    # Validate critical config
    _validate(cfg)

    return cfg


def _apply_yaml(cfg: BotConfig, raw: dict):
    """Apply YAML values to config (strategy/risk params only)."""
    cfg.mode = raw.get("mode", cfg.mode)
    cfg.scan_interval = raw.get("scan_interval", cfg.scan_interval)
    cfg.auto_trade = raw.get("auto_wayv", raw.get("auto_trade", cfg.auto_trade))
    cfg.platforms = raw.get("platforms", cfg.platforms)
    cfg.research_sources = raw.get("research_sources", cfg.research_sources)

    # Risk params from YAML
    risk_raw = raw.get("risk", {})
    if risk_raw:
        cfg.risk.max_daily_loss_usd = risk_raw.get("max_daily_loss_usd", cfg.risk.max_daily_loss_usd)
        cfg.risk.max_position_usd = risk_raw.get("max_position_usd", cfg.risk.max_position_usd)
        cfg.risk.max_open_positions = risk_raw.get("max_open_positions", cfg.risk.max_open_positions)
        cfg.risk.max_consecutive_losses = risk_raw.get("max_consecutive_losses", cfg.risk.max_consecutive_losses)
        cfg.risk.kelly_fraction = risk_raw.get("kelly_fraction", cfg.risk.kelly_fraction)
        cfg.risk.cooldown_seconds = risk_raw.get("cooldown_seconds", cfg.risk.cooldown_seconds)
        cfg.risk.min_ev_threshold = risk_raw.get("min_ev_threshold", cfg.risk.min_ev_threshold)
        cfg.risk.min_edge_bps = risk_raw.get("min_edge_bps", cfg.risk.min_edge_bps)


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

    for w in warnings:
        logger.warning(f"CONFIG: {w}")
