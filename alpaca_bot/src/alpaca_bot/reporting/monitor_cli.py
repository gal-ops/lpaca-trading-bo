"""Lightweight monitoring CLI (spec section 14). Takes an already-fetched,
already-sanitized `broker_snapshot` dict (never raw credentials or the
broker client itself) plus the persistence DB, and renders a plain-text
status report. Never prints ALPACA_API_KEY/ALPACA_SECRET_KEY or anything
resembling them -- callers must not put secrets in `broker_snapshot`."""

from __future__ import annotations

from alpaca_bot.persistence.db import Database

_FORBIDDEN_SNAPSHOT_KEYS = {"api_key", "secret_key", "alpaca_api_key", "alpaca_secret_key"}


def render_status(db: Database, broker_snapshot: dict) -> str:
    for key in broker_snapshot:
        if key.lower() in _FORBIDDEN_SNAPSHOT_KEYS:
            raise ValueError(f"refusing to render a snapshot containing a credential-looking key: {key}")

    lines = ["=" * 60, "ALPACA BOT STATUS", "=" * 60]

    lines.append(f"Account status:       {broker_snapshot.get('account_status', 'unknown')}")
    lines.append(f"Feed type:            {broker_snapshot.get('feed_type', 'unknown')}")
    lines.append(f"Market data healthy:  {broker_snapshot.get('market_data_healthy', 'unknown')}")
    lines.append(f"Trading stream healthy: {broker_snapshot.get('trading_stream_healthy', 'unknown')}")
    lines.append(f"Equity:               ${broker_snapshot.get('equity', 0):,.2f}")
    lines.append(f"Cash:                 ${broker_snapshot.get('cash', 0):,.2f}")
    lines.append(f"Gross exposure:       {broker_snapshot.get('gross_exposure_pct', 0) * 100:.1f}%")

    equity_regime = db.latest_regime("equity")
    crypto_regime = db.latest_regime("crypto")
    lines.append(f"Equity regime:        {equity_regime['regime'] if equity_regime else 'unknown'}")
    lines.append(f"Crypto regime:        {crypto_regime['regime'] if crypto_regime else 'unknown'}")

    discovered = db.query_one("SELECT COUNT(*) AS n FROM assets")
    tradable = db.query_one("SELECT COUNT(*) AS n FROM assets WHERE tradable = 1")
    lines.append(f"Universe:             {discovered['n'] if discovered else 0} discovered, "
                 f"{tradable['n'] if tradable else 0} tradable")

    top_candidates = db.query(
        "SELECT symbol, strategy, calibrated_probability FROM signals "
        "WHERE accepted = 1 ORDER BY ts DESC LIMIT 5"
    )
    lines.append("Top recent accepted candidates:")
    if top_candidates:
        for c in top_candidates:
            prob = f"{c['calibrated_probability']:.2%}" if c["calibrated_probability"] is not None else "n/a"
            lines.append(f"  - {c['symbol']} ({c['strategy']}) prob={prob}")
    else:
        lines.append("  (none)")

    accepted_count = db.query_one("SELECT COUNT(*) AS n FROM signals WHERE accepted = 1")
    rejected_count = db.query_one("SELECT COUNT(*) AS n FROM signals WHERE accepted = 0")
    lines.append(f"Signals: {accepted_count['n'] if accepted_count else 0} accepted, "
                 f"{rejected_count['n'] if rejected_count else 0} rejected")

    open_positions = db.open_positions()
    open_orders = db.open_orders()
    lines.append(f"Open positions:       {len(open_positions)}")
    for p in open_positions:
        lines.append(f"  - {p['symbol']}: {p['qty']} @ avg ${p['avg_entry_price']}")
    lines.append(f"Open orders:          {len(open_orders)}")

    latest_pnl = db.query_one("SELECT * FROM pnl_snapshots ORDER BY ts DESC LIMIT 1")
    if latest_pnl:
        lines.append(f"Realized P&L today:   {latest_pnl['realized_pnl_today']}")
        lines.append(f"Unrealized P&L:       {latest_pnl['unrealized_pnl']}")

    latest_risk_event = db.query_one("SELECT * FROM risk_events ORDER BY ts DESC LIMIT 1")
    if latest_risk_event:
        lines.append(f"Last risk event:      {latest_risk_event['event_type']} at {latest_risk_event['ts']} "
                     f"(flatten={bool(latest_risk_event['should_flatten'])})")
    else:
        lines.append("Last risk event:      none")

    buckets = db.query("SELECT bucket_key, n_examples, disabled, model_version FROM calibration_buckets")
    lines.append(f"Model buckets:        {len(buckets)}")
    for b in buckets:
        status = "DISABLED" if b["disabled"] else "active"
        lines.append(f"  - {b['bucket_key']}: n={b['n_examples']} version={b['model_version']} [{status}]")

    lines.append("=" * 60)
    return "\n".join(lines)
