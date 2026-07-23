-- SQLite schema: source of truth for everything the bot persists
-- (spec section 13). Excel is a generated report, never authoritative.

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT,
    asset_class TEXT NOT NULL,
    exchange TEXT,
    status TEXT NOT NULL,
    tradable INTEGER NOT NULL,
    fractionable INTEGER NOT NULL,
    marginable INTEGER NOT NULL,
    shortable INTEGER NOT NULL,
    easy_to_borrow INTEGER NOT NULL,
    maintenance_margin_requirement REAL,
    min_order_increment REAL,
    min_trade_increment REAL,
    price_increment REAL,
    last_checked TEXT NOT NULL,
    exclusion_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_symbol ON assets(symbol);
CREATE INDEX IF NOT EXISTS idx_assets_class ON assets(asset_class);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,       -- '1Min','5Min','15Min','1Hour','1Day'
    ts TEXT NOT NULL,              -- ISO8601 UTC bar timestamp
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    vwap REAL,
    PRIMARY KEY (symbol, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS features (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (symbol, ts, feature_name)
);

CREATE TABLE IF NOT EXISTS regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_class TEXT NOT NULL,      -- 'equity' or 'crypto'
    ts TEXT NOT NULL,
    regime TEXT NOT NULL,
    inputs_json TEXT NOT NULL       -- feature snapshot used to classify
);
CREATE INDEX IF NOT EXISTS idx_regimes_class_ts ON regimes(asset_class, ts);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction TEXT NOT NULL,        -- 'long' or 'short'
    regime TEXT,
    entry REAL,
    stop REAL,
    target REAL,
    max_holding_seconds REAL,
    feature_snapshot_json TEXT,
    raw_model_scores_json TEXT,
    calibrated_probability REAL,
    expected_value_after_costs REAL,
    accepted INTEGER NOT NULL,      -- 1 = accepted (order attempted), 0 = rejected
    rejection_reasons_json TEXT,
    model_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_signals_accepted ON signals(accepted);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    side TEXT NOT NULL,             -- 'buy' or 'sell'
    order_type TEXT NOT NULL,
    qty REAL,
    limit_price REAL,
    time_in_force TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- 'new','partial_fill','fill','canceled','rejected',...
    raw_json TEXT,
    FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    ts TEXT NOT NULL,
    FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id)
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    current_price REAL,
    market_value REAL,
    unrealized_pl REAL,
    unrealized_plpc REAL,
    opened_at TEXT,
    strategy TEXT,
    stop REAL,
    target REAL,
    max_holding_deadline TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl_today REAL,
    unrealized_pnl REAL,
    gross_exposure_pct REAL,
    open_positions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pnl_ts ON pnl_snapshots(ts);

CREATE TABLE IF NOT EXISTS risk_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT
);

CREATE TABLE IF NOT EXISTS reconnects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    feed TEXT NOT NULL,             -- 'trading_stream','market_data_stream'
    reason TEXT
);

CREATE TABLE IF NOT EXISTS calibration_buckets (
    bucket_key TEXT PRIMARY KEY,    -- e.g. 'vwap_pullback|long|equity|BULL_TREND'
    n_examples INTEGER NOT NULL DEFAULT 0,
    model_version TEXT,
    calibration_json TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT,
    updated_at TEXT NOT NULL
);
