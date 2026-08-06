-- 007_provider_sync_log.sql
-- Scheduler health and activity logging for HitRateHub.

CREATE TABLE IF NOT EXISTS provider_sync_log (
    id BIGSERIAL PRIMARY KEY,

    provider VARCHAR(40) NOT NULL DEFAULT 'prop_line',
    sport_key VARCHAR(80) NOT NULL,

    run_type VARCHAR(40) NOT NULL DEFAULT 'scheduler_cycle',
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('running', 'success', 'partial', 'failed')),

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,

    duration_seconds NUMERIC,

    events_cached INTEGER NOT NULL DEFAULT 0,
    odds_events INTEGER NOT NULL DEFAULT 0,
    market_rows INTEGER NOT NULL DEFAULT 0,
    summary_rows INTEGER NOT NULL DEFAULT 0,
    open_rows INTEGER NOT NULL DEFAULT 0,
    close_rows INTEGER NOT NULL DEFAULT 0,
    final_events INTEGER NOT NULL DEFAULT 0,
    result_rows INTEGER NOT NULL DEFAULT 0,
    stat_rows INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,

    error_message TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_provider_sync_log_started
    ON provider_sync_log (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_sync_log_status
    ON provider_sync_log (status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_sync_log_provider_sport
    ON provider_sync_log (
        provider,
        sport_key,
        started_at DESC
    );
