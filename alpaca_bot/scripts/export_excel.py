#!/usr/bin/env python3
"""Regenerates the 13-sheet Excel report from the current database
without running a trading cycle."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpaca_bot.config import get_settings, load_strategy_config  # noqa: E402
from alpaca_bot.persistence.db import Database  # noqa: E402
from alpaca_bot.reporting.excel_report import generate_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=None, help="Override the configured output path")
    args = parser.parse_args()

    settings = get_settings()
    cfg = load_strategy_config()
    output_path = args.output or cfg.get("reporting", {}).get(
        "output_path", "data/reports/alpaca_bot_report.xlsx"
    )

    db = Database(settings.database_path)
    try:
        generate_report(db, output_path)
        print(f"Report written to {output_path}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
