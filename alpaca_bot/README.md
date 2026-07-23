# alpaca_bot

Institutional-grade, multi-asset, **paper-trading-only** algorithmic trading
system for Alpaca. Built from `alpaca_master_trading_bot_prompt.md` in
verifiable phases (see that file's section 19 build sequence).

## Status: Phase 1-2 of 12 complete

Done so far:

- **Scaffold & config** (`config/default.yaml`, `config/paper.yaml`,
  `src/alpaca_bot/config.py`) -- risk/session/confidence-gate defaults from
  the spec, loaded with no path for a config file to enable live trading.
- **Paper-only broker client** (`src/alpaca_bot/broker/client.py`) --
  `paper=True` is a Python literal, not a setting. Refuses to start unless:
  `ALPACA_BASE_URL` is an Alpaca paper host, `PAPER_TRADING` is exactly the
  string `"true"`, credentials are present, the account round-trip
  succeeds, the account is `ACTIVE` and not `trading_blocked`, and
  market-data auth succeeds.
- **Dynamic asset discovery** (`src/alpaca_bot/universe/discovery.py`) --
  live equity + crypto universes from Alpaca's Assets API (no hardcoded
  tickers), `tradable_universe` filtering, equity short-eligibility check
  (`tradable` + `shortable` + `easy_to_borrow`, all strict), and crypto
  quote-currency dedup (USD > USDC > USDT, BTC-quoted pairs excluded until
  conversion accounting exists).

Not yet built (phases 3-12): SQLite persistence, universe screening
(liquidity/data-quality filters -> `eligible_now`), regime engines, the five
strategy modules, backtester, probability models/calibration, the
independent pre-trade risk validator, execution/reconciliation/kill
switches, Excel reporting, monitoring dashboard, Docker, and full test
coverage. **`main.py` currently only discovers the universe and logs
counts -- it does not evaluate signals or place any orders.**

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
```

Requires Python 3.11+ (the spec's minimum). `PAPER_TRADING` must be exactly
`true` in `.env` or the process refuses to start.

## Running

```bash
PYTHONPATH=src python -m alpaca_bot.main
```

Currently a safety/discovery smoke test only: verifies the paper account,
discovers the equity + crypto universe, prints counts, and exits without
placing any orders.

## Tests

```bash
pytest tests/unit          # mocked, no credentials needed
ruff check src tests
mypy src
```

Real-Alpaca integration tests belong in `tests/integration/` and must be
marked `@pytest.mark.integration` (excluded from the default `pytest` run
via `pyproject.toml`'s `addopts`) so they never run accidentally without
credentials.

## Known limitations at this checkpoint

- No persistence yet -- nothing survives a restart, and the spec's
  "refuse to start with unreconciled orders/positions" check can't be
  implemented until phase 3 (SQLite) exists.
- `eligible_now` (the third universe layer -- live liquidity/spread/
  volatility screening) is not implemented; only `discovered_universe` and
  `tradable_universe` exist so far.
- No regimes, strategies, models, risk validator, or execution -- this
  checkpoint cannot and does not place trades.
