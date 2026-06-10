PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS daily_features (
    hotel_id TEXT NOT NULL,
    feature_date TEXT NOT NULL,
    hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),
    day_of_week INTEGER,
    day_of_month INTEGER,
    month INTEGER,
    is_weekend INTEGER CHECK(is_weekend IN (0, 1)),
    is_holiday INTEGER CHECK(is_holiday IN (0, 1)),
    days_to_holiday INTEGER,
    season TEXT CHECK(season IN ('off_peak', 'shoulder', 'peak', 'super_peak')),
    season_ord INTEGER,
    peak_period_flag INTEGER CHECK(peak_period_flag IN (0, 1)),
    super_peak_flag INTEGER CHECK(super_peak_flag IN (0, 1)),
    hotel_star INTEGER CHECK(hotel_star BETWEEN 1 AND 5),
    hotel_id_te REAL,
    dsec_market_occ REAL,
    mha_market_occ REAL,
    blended_market_demand_signal REAL,
    occ_lag_24h REAL,
    occ_lag_72h REAL,
    occ_lag_7d REAL,
    occ_rolling_7d_mean REAL,
    occ_rolling_30d_mean REAL,
    base_price REAL,
    competitor_price REAL,
    price_ratio REAL,
    base_price_lag_24h REAL,
    border_flow REAL,
    flight_ferry REAL,
    visitors_stats REAL,
    zhuhai_saturation REAL,
    event_ticket_sales REAL,
    event_density REAL,
    days_to_next_event INTEGER,
    weather_score REAL,
    temperature REAL,
    rain_prob REAL,
    ota_booking_pace REAL,
    holiday REAL,
    demand_target REAL,
    actual_occupancy REAL,
    baseline_predicted REAL,
    target_finalized_at TEXT,
    etl_run_id TEXT,
    data_completeness REAL CHECK(data_completeness BETWEEN 0 AND 1),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (hotel_id, feature_date, hour)
);

CREATE INDEX IF NOT EXISTS idx_features_date ON daily_features(feature_date);
CREATE INDEX IF NOT EXISTS idx_features_hotel ON daily_features(hotel_id);
CREATE INDEX IF NOT EXISTS idx_features_target ON daily_features(demand_target) WHERE demand_target IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_features_etl_run ON daily_features(etl_run_id);

CREATE TABLE IF NOT EXISTS hotel_metadata (
    hotel_id TEXT PRIMARY KEY,
    hotel_name TEXT,
    hotel_star INTEGER CHECK(hotel_star BETWEEN 1 AND 5),
    market_segment TEXT CHECK(market_segment IN ('3-4_star', '5_star')),
    total_rooms INTEGER,
    location TEXT,
    onboarded_at TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS market_signals (
    signal_date TEXT NOT NULL,
    signal_hour INTEGER NOT NULL CHECK(signal_hour BETWEEN 0 AND 23),
    market_segment TEXT NOT NULL,
    dsec_occ REAL,
    mha_occ REAL,
    blended_signal REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (signal_date, signal_hour, market_segment)
);

CREATE TABLE IF NOT EXISTS etl_run_log (
    etl_run_id TEXT PRIMARY KEY,
    stage TEXT CHECK(stage IN ('extract', 'transform', 'load', 'all')),
    mode TEXT CHECK(mode IN ('incremental', 'backfill', 'manual')),
    start_date TEXT,
    end_date TEXT,
    status TEXT CHECK(status IN ('success', 'partial', 'failed')),
    n_rows_in INTEGER,
    n_rows_out INTEGER,
    error_count INTEGER DEFAULT 0,
    duration_sec REAL,
    started_at TEXT,
    finished_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS etl_anomaly_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etl_run_id TEXT,
    hotel_id TEXT,
    feature_date TEXT,
    feature_name TEXT,
    feature_value REAL,
    z_score REAL,
    severity TEXT CHECK(severity IN ('low', 'medium', 'high')),
    detected_at TEXT DEFAULT (datetime('now'))
);

