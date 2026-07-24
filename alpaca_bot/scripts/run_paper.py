#!/usr/bin/env python3
"""Runs one paper-trading cycle. Intended to be invoked on a schedule
(cron, GitHub Actions, etc.) -- this process does not loop internally."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpaca_bot.main import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
