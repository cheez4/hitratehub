-- 008_verified_bets.sql
-- Adds provider linkage and verification labels to Personal Hub bets.

ALTER TABLE user_bets
    ADD COLUMN IF NOT EXISTS verification_type VARCHAR(20);

ALTER TABLE user_bet_legs
    ADD COLUMN IF NOT EXISTS provider_market_id BIGINT,
    ADD COLUMN IF NOT EXISTS provider_summary_id BIGINT,
    ADD COLUMN IF NOT EXISTS provider_event_id VARCHAR(80),
    ADD COLUMN IF NOT EXISTS provider_market_key VARCHAR(120),
    ADD COLUMN IF NOT EXISTS verification_type VARCHAR(20);

CREATE INDEX IF NOT EXISTS idx_user_bet_legs_provider_market
    ON user_bet_legs (provider_market_id);

CREATE INDEX IF NOT EXISTS idx_user_bet_legs_provider_result_match
    ON user_bet_legs (
        provider_event_id,
        provider_market_key,
        player_name,
        ou,
        line
    );

CREATE INDEX IF NOT EXISTS idx_user_bets_verification_type
    ON user_bets (user_id, verification_type, created_at DESC);
