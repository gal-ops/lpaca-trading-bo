# Alpaca Trading Bot

Paper-trades a watchlist of stocks and crypto pairs on Alpaca using an EMA/RSI
strategy, runs every 5 minutes via GitHub Actions (24/7 — crypto never sleeps),
and logs every buy/sell to a Google Sheet with the reason, price, and realized
P&L, plus a live win/loss summary.

## 1. Alpaca paper account

1. Sign up at https://alpaca.markets and open a **paper trading** account.
2. Generate an API key + secret from the paper trading dashboard.

## 2. Google Sheets logging (~5 minutes)

1. Go to https://console.cloud.google.com, create a new project (or reuse one).
2. In "APIs & Services" → "Library", enable:
   - **Google Sheets API**
   - **Google Drive API**
3. Go to "IAM & Admin" → "Service Accounts" → "Create Service Account".
   Name it anything (e.g. `trading-bot`). No special role is needed.
4. Open the new service account → "Keys" → "Add Key" → "Create new key" → JSON.
   This downloads a `.json` file — keep it private.
5. Copy the service account's email address (looks like
   `trading-bot@your-project.iam.gserviceaccount.com`).
6. Create a new Google Sheet (blank is fine). Click "Share" and give that
   service account email **Editor** access.
7. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

The bot will auto-create a `Trades` tab (one row per buy/sell, with the
reason and realized P&L) and a `Summary` tab (total trades, wins, losses,
win rate %, total P&L — computed with live Sheets formulas).

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
GOOGLE_SERVICE_ACCOUNT_JSON={"type": "service_account", ...paste the whole JSON key here, one line...}
GOOGLE_SHEET_ID=your_sheet_id
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
- `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the entire JSON key file contents)
- `GOOGLE_SHEET_ID`

The workflow in `.github/workflows/trading_bot.yml` runs every 5 minutes via
cron, and can also be triggered manually from the Actions tab
("Run workflow").

## Strategy notes

- EMA9/EMA21 crossover + RSI14, same core signal for stocks and crypto.
- When more symbols signal "buy" than there are open position slots
  (max 6 by default), candidates are ranked by RSI/EMA momentum strength and
  only the strongest are taken.
- Stop-loss at -5%, take-profit at +12%, checked every cycle regardless of
  the strategy signal.
- Config (watchlists, thresholds, risk sizing) lives in `config.py`.

## Security

This is a fresh repo specifically because an older bot
(`~/trading_bot`) had a GitHub personal access token embedded in its git
remote URL in plaintext. If you still have that repo, rotate/revoke that
token from GitHub → Settings → Developer settings → Personal access tokens.
