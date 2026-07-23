"""Tiny persisted state for the account-drawdown stop.

The bot is stateless per run (GitHub Actions spins up fresh each cycle), but
the drawdown stop needs to remember the account's high-water mark and whether
the stop has already fired today. This stores just that, as JSON, in
config.STATE_FILE -- which the workflow commits back to the repo alongside
trades.xlsx. Fail-safe: any read error returns a fresh default so a missing/
corrupt file can never crash a cycle.
"""
import json
import config

_DEFAULT = {"high_water_mark": 0.0, "halt_date": ""}


def load() -> dict:
    try:
        with open(config.STATE_FILE) as f:
            data = json.load(f)
        return {
            "high_water_mark": float(data.get("high_water_mark", 0.0) or 0.0),
            "halt_date": str(data.get("halt_date", "") or ""),
        }
    except Exception:
        return dict(_DEFAULT)


def save(state: dict) -> None:
    try:
        with open(config.STATE_FILE, "w") as f:
            json.dump({
                "high_water_mark": float(state.get("high_water_mark", 0.0) or 0.0),
                "halt_date": str(state.get("halt_date", "") or ""),
            }, f)
    except Exception:
        pass  # never let a state-write failure break a trading cycle
