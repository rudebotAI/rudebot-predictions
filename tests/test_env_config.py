"""
Unit tests for environment-aware config loader.
Run: pytest tests/ -v
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfigLoader:
    def test_defaults_without_yaml(self):
        with patch.dict(os.environ, {}, clear=True):
            from env_config import load_config
            cfg = load_config("/nonexistent/config.yaml")
            assert cfg.mode == "paper"
            assert cfg.risk.max_daily_loss_usd == 100.0
            assert cfg.risk.kelly_fraction == 0.25

    def test_env_overrides(self):
        env = {
            "BOT_MODE": "paper",
            "KALSHI_EMAIL": "test@example.com",
            "KALSHI_API_KEY": "secret123",
            "MAX_DAILY_LOSS_USD": "200",
            "KELLY_FRACTION": "0.15",
        }
        with patch.dict(os.environ, env, clear=False):
            from env_config import load_config
            cfg = load_config("/nonexistent/config.yaml")
            assert cfg.kalshi.email == "test@example.com"
            assert cfg.kalshi.api_key == "secret123"
            assert cfg.risk.max_daily_loss_usd == 200.0
            assert cfg.risk.kelly_fraction == 0.15

    def test_yaml_loading(self):
        yaml_content = """
mode: paper
scan_interval: 60
risk:
  max_daily_loss_usd: 150
  max_open_positions: 3
platforms:
  - kalshi
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            from env_config import load_config
            cfg = load_config(f.name)
            assert cfg.scan_interval == 60
            assert cfg.risk.max_daily_loss_usd == 150
            assert cfg.risk.max_open_positions == 3
            os.unlink(f.name)

    def test_secrets_not_from_yaml(self):
        """Secrets should come from env, not yaml."""
        yaml_content = """
kalshi:
  email: "yaml@bad.com"
  api_key: "yaml_secret"
"""
        env = {"KALSHI_EMAIL": "env@good.com", "KALSHI_API_KEY": "env_secret"}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with patch.dict(os.environ, env, clear=False):
                from env_config import load_config
                cfg = load_config(f.name)
                # Env vars should win for secrets
                assert cfg.kalshi.email == "env@good.com"
                assert cfg.kalshi.api_key == "env_secret"
            os.unlink(f.name)
