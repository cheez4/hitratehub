-- 004_personal_hub.sql
-- Expands the existing personal betting tables without removing or renaming
-- any columns used by System Watch.

BEGIN;

ALTER TABLE user_bets
    ADD COLUMN IF NOT EXISTS sport VARCHAR(40),
    ADD COLUMN IF NOT EXISTS league VARCHAR(80),
    ADD COLUMN IF NOT EXISTS source VARCHAR(50),
    ADD COLUMN IF NOT EXISTS title VARCHAR(200),
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS settled_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS is_manual BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE user_bet_legs
    ADD COLUMN IF NOT EXISTS sport VARCHAR(40),
    ADD COLUMN IF NOT EXISTS league VARCHAR(80),
    ADD COLUMN IF NOT EXISTS selection_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS selection_name VARCHAR(200),
    ADD COLUMN IF NOT EXISTS team_name VARCHAR(120),
    ADD COLUMN IF NOT EXISTS opponent VARCHAR(120),
    ADD COLUMN IF NOT EXISTS result VARCHAR(20),
    ADD COLUMN IF NOT EXISTS sort_order INTEGER;

CREATE INDEX IF NOT EXISTS idx_user_bets_user_time
    ON user_bets (user_id, bet_time DESC);

CREATE INDEX IF NOT EXISTS idx_user_bets_user_status
    ON user_bets (user_id, status);

CREATE INDEX IF NOT EXISTS idx_user_bets_bankroll
    ON user_bets (bankroll_id);

CREATE INDEX IF NOT EXISTS idx_user_bet_legs_bet
    ON user_bet_legs (user_bet_id, sort_order);

COMMIT;
