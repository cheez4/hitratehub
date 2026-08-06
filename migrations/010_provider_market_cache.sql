-- 010_provider_market_cache.sql
CREATE TABLE IF NOT EXISTS provider_market_cache (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    home_team TEXT,
    away_team TEXT,
    commence_time TIMESTAMPTZ,
    clean_player_name TEXT NOT NULL,
    raw_player_name TEXT NOT NULL,
    market_key VARCHAR(120) NOT NULL,
    market_label TEXT NOT NULL,
    period VARCHAR(80) NOT NULL DEFAULT '',
    outcome_name TEXT NOT NULL,
    line NUMERIC,
    line_key NUMERIC GENERATED ALWAYS AS (
        COALESCE(line, -999999999)
    ) STORED,
    summary_id BIGINT,
    books_available INTEGER NOT NULL DEFAULT 0,
    best_odds INTEGER,
    best_bookmaker_key VARCHAR(100),
    best_bookmaker_title TEXT,
    worst_odds INTEGER,
    average_odds INTEGER,
    is_main BOOLEAN NOT NULL DEFAULT FALSE,
    books JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_updated_at TIMESTAMPTZ,
    cache_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        clean_player_name,
        market_key,
        period,
        outcome_name,
        line_key
    )
);

CREATE INDEX IF NOT EXISTS idx_provider_market_cache_event_player
ON provider_market_cache (provider_event_id, clean_player_name);

CREATE INDEX IF NOT EXISTS idx_provider_market_cache_event_market
ON provider_market_cache (
    provider_event_id, market_key, outcome_name, line
);

CREATE INDEX IF NOT EXISTS idx_provider_market_cache_upcoming
ON provider_market_cache (sport_key, commence_time);
