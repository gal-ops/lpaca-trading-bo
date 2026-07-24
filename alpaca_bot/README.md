# alpaca_bot

Institutional-grade, multi-asset, **paper-trading-only** algorithmic
trading system for Alpaca. Built from `alpaca_master_trading_bot_prompt.md`
in verifiable phases (spec section 19's build sequence). All 12 phases
are implemented; see **Limitations** below for exactly what that does and
does not mean in practice.

## What this is, and isn't

This system will **discover the tradable universe, screen it, classify
market regimes, generate strategy candidates, and log every decision** --
but on a fresh account with no trade history, it will **not place real
orders**, by design. The 85% confidence gate (spec section 7) requires
200+ real, outcome-labeled examples per strategy/direction/asset-class/
regime bucket before it will ever accept a signal; until then every
candidate is logged as `RESEARCH_ONLY_INSUFFICIENT_SAMPLE`. That is
correct behavior, not a bug -- read this before assuming something is
broken because the bot "isn't trading."

**This system makes no claim of profitability.** Nothing here should be
interpreted as investment advice. Backtest and paper results are
sanity checks on strategy logic, not predictions.

## Architecture

```
src/alpaca_bot/
├── main.py            entry point: one full cycle per invocation
├── config.py           settings + config/*.yaml loader
├── broker/             paper-only enforced Alpaca client (the safety gate)
├── universe/           dynamic asset discovery + eligibility screening
├── data/                batched historical bar fetching
├── features/            cheap liquidity/volatility features
├── regimes/              equity + crypto rule-based regime classifiers
├── strategies/           the 5 strategies from spec section 5
├── backtester/           event-driven backtester + performance metrics
├── models/                probability model training/calibration/gate
├── risk/                  independent pre-trade risk validator
├── execution/            order manager, reconciliation, kill switches
├── reporting/            13-sheet Excel report + monitoring CLI
└── persistence/          SQLite schema + access layer (source of truth)
```

Every module has unit tests (`tests/unit/`) using mocked Alpaca calls --
no real credentials are needed to run the test suite.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
```

Requires Python 3.11+. `PAPER_TRADING` must be the exact string `true` in
`.env`, and `ALPACA_BASE_URL` must be an Alpaca paper host -- the process
refuses to start otherwise (see `broker/client.py`).

**Reset your paper account balance to approximately your intended live
capital before running this for real.** The dollar-denominated risk
caps in `config/paper.yaml` are sized for ~$540; on Alpaca's default
$100k paper balance the percentage-based caps and the dollar caps
measure completely different things, and the daily-loss/drawdown stops
will misfire.

## Running

```bash
# One full cycle: discovery -> screening -> regimes -> strategies ->
# confidence gate -> risk validator -> execution -> reconciliation ->
# kill switches -> snapshot -> Excel report.
PYTHONPATH=src python -m alpaca_bot.main

# Or via the wrapper script (same thing):
python scripts/run_paper.py
```

`main.py` runs **one cycle per invocation** -- it does not loop
internally. Schedule it externally (cron, GitHub Actions, Docker with an
external scheduler) at whatever cadence you want.

```bash
# Regenerate the Excel report without running a trading cycle:
python scripts/export_excel.py

# Backtest strategies against real historical daily bars:
python scripts/backtest.py --symbols AAPL MSFT NVDA TSLA --days 400

# Train/calibrate probability models from real, outcome-labeled signal
# history (does nothing useful until enough real trades have accumulated):
python scripts/train_models.py
```

## Docker

```bash
docker compose build
ALPACA_API_KEY=... ALPACA_SECRET_KEY=... docker compose run --rm alpaca-bot
```

Runs one cycle per container invocation, same as the script -- schedule
externally.

## Tests

```bash
pytest tests/unit          # fully mocked, no credentials needed
ruff check src tests
mypy src
```

Real-Alpaca integration tests belong in `tests/integration/` and must be
marked `@pytest.mark.integration` (excluded from the default `pytest` run
via `pyproject.toml`'s `addopts`) so they never run accidentally without
credentials.

## Limitations (read before relying on this for anything)

These are the gaps between this build and the spec's full ideal,
disclosed explicitly rather than left implicit:

- **No live WebSocket market-data or trading stream.** Spec section 3
  calls for streaming; this build uses batched REST calls only. Every
  "fresh" quote the pre-trade risk validator sees is the latest fetched
  bar, not a true real-time tick. This is the single biggest gap versus
  the spec's ideal, and directly affects the accuracy of the freshness/
  staleness checks (spec section 8, checks 9 and 16).
- **The backtester replays daily bars, not tick-level bid/ask data.**
  Intraday-specific strategies (opening-range breakout, VWAP pullback/
  reversion, news momentum) run in a degraded single-bar-per-day
  approximation during backtests. Every `BacktestResult` carries this as
  a literal `limitation_notice` string.
- **No real probability models exist yet.** A brand-new account has zero
  outcome-labeled signals, so every calibration bucket is empty and every
  signal is rejected as `RESEARCH_ONLY_INSUFFICIENT_SAMPLE`. Run
  `scripts/train_models.py` periodically as real trade history
  accumulates; nothing will train until a bucket clears 200 examples.
- **Realized per-trade P&L in the Excel report is not FIFO-matched.**
  The `Trades` sheet lists raw fills; computing true realized P&L per
  round-trip trade needs FIFO lot-matching that isn't built. Strategy
  win/loss stats instead use `signals.outcome_label`, which execution
  fills in once a trade closes.
- **The catalyst/news strategy consumes a news feed it doesn't fetch.**
  `strategies/news_momentum.py` expects `context.news_score`/
  `news_headline` to already be populated; a real news client (fetch,
  dedupe, classify sentiment) is not implemented in this build.
- **SIP vs. IEX equity data distinction is not enforced.** The spec
  requires either a separately-calibrated IEX model or disabling equity
  execution when SIP data isn't available; this build does not
  distinguish between the two feed types at runtime. Requires an Alpaca
  market-data subscription to address properly.
- **Correlated-cluster exposure is a placeholder.** The risk validator's
  `correlated_cluster_exposure_usd` check exists and is enforced, but
  nothing yet computes real sector/BTC-correlation clustering to feed it
  -- callers must supply it.
- **No hidden Markov model / calibrated regime classifier.** Only the
  spec's transparent rule-based regime engines are implemented (as the
  spec explicitly allows/expects for an initial version).

## Requires real credentials or a paid subscription to fully validate

- Alpaca SIP market data (paid) to properly implement/validate the SIP-
  vs-IEX distinction above.
- A real news/data feed for the catalyst momentum strategy.
- 60-90 market days of live paper trading (spec section 12) before any
  claim of paper-release-readiness is meaningful -- this cannot be
  fast-forwarded.
