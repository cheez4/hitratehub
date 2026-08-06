-- 009_provider_adaptive_odds_sync.sql
-- Track the last successful per-event odds refresh so the scheduler can
-- use an adaptive request cadence.

ALTER TABLE provider_events
    ADD COLUMN IF NOT EXISTS last_odds_sync_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_provider_events_odds_due
    ON provider_events (
        sport_key,
        commence_time,
        last_odds_sync_at
    )
    WHERE live = FALSE;
