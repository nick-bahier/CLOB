"""
Configuration loader.

Loads from config/default.json with env var overrides.
Env vars take the form CLOB_<SECTION>_<KEY> in uppercase.

Example env overrides:
  CLOB_KALSHI_MARKET_TICKER=PRES-2028-DEM
  CLOB_POLYMARKET_TOKEN_ID=12345...
  CLOB_KALSHI_USE_FIXTURE=true
  CLOB_SERVER_PORT=8765
  CLOB_STALENESS_THRESHOLD=5.0
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_CONFIG = CONFIG_DIR / "default.json"


@dataclass
class KalshiConfig:
    market_ticker: str = ""
    api_key_id: str = ""
    private_key_path: str = ""
    use_demo: bool = False
    use_fixture: bool = False
    fixture_path: str = "fixtures/kalshi.json"


@dataclass
class PolymarketConfig:
    token_id: str = ""
    condition_id: str = ""
    use_fixture: bool = False
    fixture_path: str = "fixtures/polymarket.json"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    static_dir: str = "client"


@dataclass
class AppConfig:
    market_name: str = "U.S. Presidential Election Winner"
    outcome: str = "Yes"
    stale_threshold_s: float = 5.0
    staleness_check_interval_s: float = 1.0
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Load config from JSON file, then apply env var overrides.

    Priority: env vars > config file > defaults
    """
    config = AppConfig()

    # Load from JSON file
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if path.exists():
        try:
            with open(path, "r") as f:
                data = json.load(f)
            _apply_dict(config, data)
            logger.info(f"Loaded config from {path}")
        except Exception:
            logger.exception(f"Failed to load config from {path}, using defaults")
    else:
        logger.warning(f"Config file not found: {path}, using defaults")

    # Apply env var overrides
    _apply_env_overrides(config)

    return config


def _apply_dict(config: AppConfig, data: dict) -> None:
    """Apply a dict of values to the config dataclass."""
    for key, value in data.items():
        if key == "kalshi" and isinstance(value, dict):
            for k, v in value.items():
                if hasattr(config.kalshi, k):
                    setattr(config.kalshi, k, v)
        elif key == "polymarket" and isinstance(value, dict):
            for k, v in value.items():
                if hasattr(config.polymarket, k):
                    setattr(config.polymarket, k, v)
        elif key == "server" and isinstance(value, dict):
            for k, v in value.items():
                if hasattr(config.server, k):
                    setattr(config.server, k, v)
        elif hasattr(config, key):
            setattr(config, key, value)


def _apply_env_overrides(config: AppConfig) -> None:
    """
    Override config values from environment variables.
    Format: CLOB_<SECTION>_<KEY>=value

    Examples:
        CLOB_KALSHI_MARKET_TICKER=PRES-2028-DEM
        CLOB_KALSHI_API_KEY_ID=abc123
        CLOB_KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
        CLOB_KALSHI_USE_DEMO=true
        CLOB_KALSHI_USE_FIXTURE=true
        CLOB_POLYMARKET_TOKEN_ID=12345...
        CLOB_POLYMARKET_USE_FIXTURE=true
        CLOB_SERVER_PORT=8765
        CLOB_STALE_THRESHOLD_S=5.0
        CLOB_MARKET_NAME=My Market
    """
    env_map = {
        # Top-level
        "CLOB_MARKET_NAME": (config, "market_name", str),
        "CLOB_OUTCOME": (config, "outcome", str),
        "CLOB_STALE_THRESHOLD_S": (config, "stale_threshold_s", float),
        "CLOB_STALENESS_CHECK_INTERVAL_S": (config, "staleness_check_interval_s", float),
        # Kalshi
        "CLOB_KALSHI_MARKET_TICKER": (config.kalshi, "market_ticker", str),
        "CLOB_KALSHI_API_KEY_ID": (config.kalshi, "api_key_id", str),
        "CLOB_KALSHI_PRIVATE_KEY_PATH": (config.kalshi, "private_key_path", str),
        "CLOB_KALSHI_USE_DEMO": (config.kalshi, "use_demo", _parse_bool),
        "CLOB_KALSHI_USE_FIXTURE": (config.kalshi, "use_fixture", _parse_bool),
        "CLOB_KALSHI_FIXTURE_PATH": (config.kalshi, "fixture_path", str),
        # Polymarket
        "CLOB_POLYMARKET_TOKEN_ID": (config.polymarket, "token_id", str),
        "CLOB_POLYMARKET_CONDITION_ID": (config.polymarket, "condition_id", str),
        "CLOB_POLYMARKET_USE_FIXTURE": (config.polymarket, "use_fixture", _parse_bool),
        "CLOB_POLYMARKET_FIXTURE_PATH": (config.polymarket, "fixture_path", str),
        # Server
        "CLOB_SERVER_HOST": (config.server, "host", str),
        "CLOB_SERVER_PORT": (config.server, "port", int),
    }

    for env_key, (obj, attr, cast) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                setattr(obj, attr, cast(val))
                logger.info(f"Config override: {env_key}={val}")
            except Exception:
                logger.warning(f"Invalid env override: {env_key}={val}")


def _parse_bool(val: str) -> bool:
    return val.lower() in ("true", "1", "yes")