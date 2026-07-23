"""Configuration loading. Deliberately has NO way to enable live trading --
that gate is enforced independently in broker/client.py regardless of what
appears here, so a config-file edit alone can never move real money."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_strategy_config(path: str | None = None) -> dict[str, Any]:
    """Loads config/default.yaml merged with an override file (paper.yaml
    by default, or ALPACA_BOT_CONFIG env if set)."""
    default_path = REPO_ROOT / "config" / "default.yaml"
    override_rel: str = path if path is not None else os.getenv("ALPACA_BOT_CONFIG", "config/paper.yaml")
    override_path = REPO_ROOT / override_rel

    with open(default_path) as f:
        cfg = yaml.safe_load(f) or {}
    if override_path.exists():
        with open(override_path) as f:
            override = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, override)
    return cfg


class Settings:
    """Environment-derived settings. ALPACA_API_KEY/SECRET_KEY are read
    directly from the environment and never logged or persisted anywhere."""

    def __init__(self) -> None:
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        # Intentionally a strict string comparison, not truthy/bool parsing --
        # "True", "1", "yes" etc. must NOT accidentally satisfy this. Spec
        # section 1, rule 5/6: must be exactly "true".
        self.paper_trading_flag = os.getenv("PAPER_TRADING", "")
        self.database_path = os.getenv("DATABASE_PATH", "data/state/alpaca_bot.db")

    def __repr__(self) -> str:
        # Never include api_key/secret_key -- secret redaction is a hard
        # requirement (spec section 16), and repr() is what shows up in
        # logs/tracebacks if this object is ever printed.
        return (
            f"Settings(base_url={self.base_url!r}, "
            f"paper_trading_flag={self.paper_trading_flag!r}, "
            f"database_path={self.database_path!r})"
        )


def get_settings() -> Settings:
    return Settings()
