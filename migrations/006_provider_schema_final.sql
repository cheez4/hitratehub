-- 006_provider_schema_final.sql
-- Final provider schema for HitRateHub.
--
-- WARNING:
-- This resets provider test tables and deletes existing provider test data.

BEGIN;

DROP TABLE IF EXISTS provider_market_summary CASCADE;
DROP TABLE IF EXISTS provider_market_snapshots CASCADE;
DROP TABLE IF EXISTS provider_market_history CASCADE;
DROP TABLE IF EXISTS provider_markets_current CASCADE;
DROP TABLE IF EXISTS provider_markets CASCADE;
DROP TABLE IF EXISTS provider_results CASCADE;
DROP TABLE IF EXISTS provider_stats CASCADE;
DROP TABLE IF EXISTS provider_events CASCADE;


CREATE TABLE provider_events (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    home_team TEXT,
    away_team TEXT,
    commence_time TIMESTAMPTZ,
    live BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(40),
    home_score NUMERIC,
    away_score NUMERIC,
    venue TEXT,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_updated_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_event_id)
);

CREATE INDEX idx_provider_events_sport_time
    ON provider_events (sport_key, commence_time);

CREATE INDEX idx_provider_events_status
    ON provider_events (status, live);


CREATE TABLE provider_markets (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    bookmaker_key VARCHAR(100) NOT NULL,
    bookmaker_title TEXT,
    market_key VARCHAR(120) NOT NULL,
    market_description TEXT,
    period VARCHAR(80) NOT NULL DEFAULT '',
    player_name TEXT NOT NULL DEFAULT '',
    outcome_name TEXT NOT NULL,
    line NUMERIC,
    line_key NUMERIC GENERATED ALWAYS AS (
        COALESCE(line, -999999999)
    ) STORED,
    odds INTEGER,
    dfs_odds_type VARCHAR(80),
    payout_multiplier NUMERIC,
    book_updated_at TIMESTAMPTZ,
    market_updated_at TIMESTAMPTZ,
    last_change_at TIMESTAMPTZ,
    source_last_update TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        bookmaker_key,
        market_key,
        period,
        player_name,
        outcome_name,
        line_key
    )
);

CREATE INDEX idx_provider_markets_event
    ON provider_markets (sport_key, provider_event_id);

CREATE INDEX idx_provider_markets_selection
    ON provider_markets (
        provider_event_id,
        market_key,
        player_name,
        outcome_name,
        line
    );

CREATE INDEX idx_provider_markets_book
    ON provider_markets (bookmaker_key, market_key);


CREATE TABLE provider_market_history (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    bookmaker_key VARCHAR(100) NOT NULL,
    bookmaker_title TEXT,
    market_key VARCHAR(120) NOT NULL,
    market_description TEXT,
    period VARCHAR(80) NOT NULL DEFAULT '',
    player_name TEXT NOT NULL DEFAULT '',
    outcome_name TEXT NOT NULL,
    line NUMERIC,
    line_key NUMERIC GENERATED ALWAYS AS (
        COALESCE(line, -999999999)
    ) STORED,
    odds INTEGER,
    dfs_odds_type VARCHAR(80),
    payout_multiplier NUMERIC,
    checkpoint VARCHAR(10) NOT NULL
        CHECK (checkpoint IN ('open', 'close')),
    source_last_update TIMESTAMPTZ,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        bookmaker_key,
        market_key,
        period,
        player_name,
        outcome_name,
        line_key,
        checkpoint
    )
);

CREATE INDEX idx_provider_history_event_checkpoint
    ON provider_market_history (
        provider_event_id,
        checkpoint
    );

CREATE INDEX idx_provider_history_selection
    ON provider_market_history (
        provider_event_id,
        market_key,
        player_name,
        outcome_name,
        line,
        bookmaker_key,
        checkpoint
    );


CREATE TABLE provider_market_summary (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    market_key VARCHAR(120) NOT NULL,
    period VARCHAR(80) NOT NULL DEFAULT '',
    player_name TEXT NOT NULL DEFAULT '',
    outcome_name TEXT NOT NULL,
    line NUMERIC,
    line_key NUMERIC GENERATED ALWAYS AS (
        COALESCE(line, -999999999)
    ) STORED,
    books_available INTEGER NOT NULL DEFAULT 0,
    best_odds INTEGER,
    best_bookmaker_key VARCHAR(100),
    best_bookmaker_title TEXT,
    worst_odds INTEGER,
    average_implied_probability NUMERIC,
    average_odds INTEGER,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        market_key,
        period,
        player_name,
        outcome_name,
        line_key
    )
);

CREATE INDEX idx_provider_summary_lookup
    ON provider_market_summary (
        provider_event_id,
        market_key,
        player_name,
        outcome_name,
        line
    );


CREATE TABLE provider_results (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    bookmaker_key VARCHAR(100) NOT NULL,
    bookmaker_title TEXT,
    market_key VARCHAR(120) NOT NULL,
    market_description TEXT,
    player_name TEXT NOT NULL DEFAULT '',
    outcome_name TEXT NOT NULL,
    line NUMERIC,
    line_key NUMERIC GENERATED ALWAYS AS (
        COALESCE(line, -999999999)
    ) STORED,
    odds INTEGER,
    resolution VARCHAR(30),
    actual_value NUMERIC,
    resolved_at TIMESTAMPTZ,
    redacted BOOLEAN NOT NULL DEFAULT FALSE,
    dfs_odds_type VARCHAR(80),
    raw_outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        bookmaker_key,
        market_key,
        player_name,
        outcome_name,
        line_key
    )
);

CREATE INDEX idx_provider_results_match
    ON provider_results (
        provider_event_id,
        market_key,
        player_name,
        outcome_name,
        line,
        resolution
    );


CREATE TABLE provider_stats (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    provider_event_id VARCHAR(80) NOT NULL,
    sport_key VARCHAR(80) NOT NULL,
    player_name TEXT NOT NULL DEFAULT '',
    team_abbr VARCHAR(20) NOT NULL DEFAULT '',
    stat_type VARCHAR(120) NOT NULL,
    stat_value NUMERIC,
    raw_stat JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        provider,
        provider_event_id,
        player_name,
        team_abbr,
        stat_type
    )
);

CREATE INDEX idx_provider_stats_lookup
    ON provider_stats (
        provider_event_id,
        player_name,
        stat_type
    );

COMMIT;
