# Alpaca Trading Bot

Paper-trades a watchlist of stocks and crypto pairs on Alpaca using an EMA/RSI
strategy, runs every 5 minutes via GitHub Actions (24/7 — crypto never sleeps),
and logs every buy/sell to an Excel file (`trades.xlsx`) with the reason,
price, and realized P&L, plus a live win/loss summary. No Google account or
billing needed — the workflow commits the updated spreadsheet straight back
into the repo after each run.

## 1. Alpaca paper account

1. Sign up at https://alpaca.markets and open a **paper trading** account.
2. Generate an API key + secret from the paper trading dashboard.

## 2. Trade log

The bot auto-creates `trades.xlsx` (in the repo root) the first time it logs
a trade, with two tabs:
- **Trades** — one row per buy/sell: timestamp, symbol, asset class, side,
  qty, price, value, reason, realized P&L ($ and %), order ID.
- **Summary** — total trades, wins, losses, win rate %, total realized P&L,
  computed with live Excel formulas so it can't drift from the trade rows.

Nothing to set up here — just make sure `trades.xlsx` is committed (it's not
gitignored) so GitHub Actions can update it run over run.

## 3. Local setup

```bash
cd alpaca-trading-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (gitignored) in this directory:

```
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Test a single cycle:

```bash
python run_once.py
```

Run continuously (local, every 5 min):

```bash
python main.py
```

## 4. Push to GitHub and run 24/7

This repo has no remote configured yet — no credentials are embedded in git
config this time. Create a **new, empty** repository on GitHub first, then:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

(Use `gh auth login` beforehand, or your normal HTTPS credential manager /
SSH key — never put a token directly in the remote URL.)

Then, in the new repo on GitHub: **Settings → Secrets and variables →
Actions → New repository secret**, and add:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

The workflow in `.github/workflows/trading_bot.yml` runs every 5 minutes via
cron, and can also be triggered manually from the Actions tab
("Run workflow"). After each run, it commits any new rows in `trades.xlsx`
straight back to the repo using the automatic `GITHUB_TOKEN` — no extra
secret needed for that part. Just download/pull the repo whenever you want
to check the spreadsheet, or view it directly on GitHub.

## Strategy notes

- EMA9/EMA21 crossover + RSI14, same core signal for stocks and crypto.
- **News research**: for every buy candidate, `news_client.py` pulls recent
  headlines from Alpaca's free News API and scores them with a keyword-based
  sentiment scan (bullish words like "beats estimates", "record revenue";
  bearish words like "downgrade", "lawsuit", "recall"). This score is folded
  into the ranking, and candidates with clearly bad news are vetoed even if
  the technical signal says buy. No paid API or extra account needed.
- When more symbols signal "buy" than there are open position slots
  (max 6 by default), candidates are ranked by RSI/EMA momentum strength
  *plus* news sentiment, and only the strongest are taken.
- Stop-loss at -5%, take-profit at +12%, checked every cycle regardless of
  the strategy signal.
- Config (watchlists, thresholds, risk sizing) lives in `config.py`.

## Security

This is a fresh repo specifically because an older bot
(`~/trading_bot`) had a GitHub personal access token embedded in its git
remote URL in plaintext. If you still have that repo, rotate/revoke that
token from GitHub → Settings → Developer settings → Personal access tokens.
