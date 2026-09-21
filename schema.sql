-- JEV-ARB Phase 1 schema. All records are paper/research records.
CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id TEXT PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    mode TEXT NOT NULL,
    config_json TEXT NOT NULL,
    safety_marker TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    sequence INTEGER,
    data_age_ms INTEGER,
    book_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_lookup
    ON market_snapshots(run_id, venue, symbol, ts_ms);

CREATE TABLE IF NOT EXISTS exchange_health (
    run_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    connected INTEGER NOT NULL,
    last_message_ms INTEGER,
    reconnects INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(run_id, venue),
    FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    detected_at_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    buy_venue TEXT NOT NULL,
    sell_venue TEXT NOT NULL,
    expected_net_profit_usd REAL NOT NULL,
    expected_net_spread_pct REAL NOT NULL,
    candidate_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_run_time
    ON candidates(run_id, detected_at_ms);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    request_at_ms INTEGER NOT NULL,
    decision_at_ms INTEGER NOT NULL,
    response_at_ms INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    confidence REAL,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_candidate_strategy
    ON decisions(candidate_id, strategy);

CREATE TABLE IF NOT EXISTS simulated_trades (
    trade_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    status TEXT NOT NULL,
    detection_at_ms INTEGER NOT NULL,
    decision_at_ms INTEGER NOT NULL,
    arrival_at_ms INTEGER NOT NULL,
    expected_pnl_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    mark_to_market_pnl_usd REAL NOT NULL,
    net_cashflow_usd REAL NOT NULL,
    filled_buy_qty REAL NOT NULL,
    filled_sell_qty REAL NOT NULL,
    trade_json TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_candidate_strategy
    ON simulated_trades(candidate_id, strategy);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    side TEXT NOT NULL,
    base_qty REAL NOT NULL,
    quote_amount REAL NOT NULL,
    vwap REAL NOT NULL,
    fee_usd REAL NOT NULL,
    fill_json TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trades(trade_id)
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    inventory_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
);

