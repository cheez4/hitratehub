"""
Sync Prop-Line data into the lean HitRateHub provider cache.

Required environment variables:
    PROP_LINE_API_KEY

Database connection is loaded through database.py.

Examples:
    python sync_prop_line.py --event-id 62542 --odds
    python sync_prop_line.py --event-id 109078 --results --stats
    python sync_prop_line.py --events
    python sync_prop_line.py --all --max-events 3
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from psycopg2.extras import Json, execute_values

from database import get_conn

from services.prop_line import PropLineClient


DEFAULT_SPORT = "baseball_mlb"

DEFAULT_MARKETS = [
    "batter_hits",
    "batter_home_runs",
    "batter_total_bases",
    "batter_rbis",
    "batter_runs",
    "pitcher_strikeouts",
    "pitcher_outs",
]

def parse_timestamp(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def number_or_none(value):
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except Exception:
        return None


def event_context_fields(event):
    context = event.get("context") or {}

    return {
        "venue": context.get("venue"),
        "provider_updated_at": parse_timestamp(
            context.get("updated_at")
            or event.get("last_update")
        ),
        "context": context,
    }


def upsert_events(conn, events):
    rows = []

    for event in events:
        extras = event_context_fields(event)

        rows.append((
            "prop_line",
            str(event.get("id")),
            event.get("sport_key"),
            event.get("home_team"),
            event.get("away_team"),
            parse_timestamp(event.get("commence_time")),
            bool(event.get("live")),
            event.get("status"),
            number_or_none(event.get("home_score")),
            number_or_none(event.get("away_score")),
            extras["venue"],
            Json(extras["context"]),
            extras["provider_updated_at"],
        ))

    if not rows:
        return 0

    sql = """
        INSERT INTO provider_events (
            provider,
            provider_event_id,
            sport_key,
            home_team,
            away_team,
            commence_time,
            live,
            status,
            home_score,
            away_score,
            venue,
            context,
            provider_updated_at
        )
        VALUES %s
        ON CONFLICT (provider, provider_event_id)
        DO UPDATE SET
            sport_key = EXCLUDED.sport_key,
            home_team = EXCLUDED.home_team,
            away_team = EXCLUDED.away_team,
            commence_time = EXCLUDED.commence_time,
            live = EXCLUDED.live,
            status = COALESCE(EXCLUDED.status, provider_events.status),
            home_score = COALESCE(EXCLUDED.home_score, provider_events.home_score),
            away_score = COALESCE(EXCLUDED.away_score, provider_events.away_score),
            venue = COALESCE(EXCLUDED.venue, provider_events.venue),
            context = CASE
                WHEN EXCLUDED.context = '{}'::jsonb
                THEN provider_events.context
                ELSE EXCLUDED.context
            END,
            provider_updated_at = COALESCE(
                EXCLUDED.provider_updated_at,
                provider_events.provider_updated_at
            ),
            last_seen_at = NOW(),
            updated_at = NOW()
    """

    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=500)

    return len(rows)


def flatten_markets(payload):
    event_id = str(payload.get("id"))
    sport_key = payload.get("sport_key")
    source_last_update = parse_timestamp(payload.get("last_update"))

    rows = []

    for bookmaker in payload.get("bookmakers") or []:
        bookmaker_key = bookmaker.get("key")
        bookmaker_title = bookmaker.get("title")
        bookmaker_update = parse_timestamp(bookmaker.get("last_update"))

        for market in bookmaker.get("markets") or []:
            market_key = market.get("key")
            market_description = market.get("description")
            market_update = parse_timestamp(market.get("last_update"))
            period = market.get("period")

            for outcome in market.get("outcomes") or []:
                player_name = (
                    outcome.get("description")
                    or (
                        outcome.get("name")
                        if market_key and market_key.startswith(("batter_", "pitcher_"))
                        else None
                    )
                )

                rows.append({
                    "provider": "prop_line",
                    "event_id": event_id,
                    "sport_key": sport_key,
                    "bookmaker_key": bookmaker_key,
                    "bookmaker_title": bookmaker_title,
                    "market_key": market_key,
                    "market_description": market_description,
                    "period": period or "",
                    "player_name": player_name or "",
                    "outcome_name": str(outcome.get("name") or ""),
                    "line": number_or_none(outcome.get("point")),
                    "odds": outcome.get("price"),
                    "dfs_odds_type": outcome.get("dfs_odds_type"),
                    "payout_multiplier": number_or_none(
                        outcome.get("payout_multiplier")
                    ),
                    "book_updated_at": parse_timestamp(
                        outcome.get("book_updated_at")
                    ) or bookmaker_update,
                    "market_updated_at": market_update,
                    "last_change_at": parse_timestamp(
                        outcome.get("last_change_at")
                    ),
                    "source_last_update": source_last_update,
                    "raw_outcome": outcome,
                })

    return rows


def dedupe_market_rows(rows):
    """
    Keep one row per provider market selection key.

    Prop-Line can occasionally return duplicate selections in one payload.
    PostgreSQL cannot update the same conflict key twice in one INSERT.
    """
    deduped = {}

    for row in rows:
        key = (
            row["provider"],
            row["event_id"],
            row["bookmaker_key"],
            row["market_key"],
            row["period"] or "",
            row["player_name"] or "",
            row["outcome_name"],
            (
                row["line"]
                if row["line"] is not None
                else Decimal("-999999999")
            ),
        )

        existing = deduped.get(key)

        if existing is None:
            deduped[key] = row
            continue

        existing_time = (
            existing.get("last_change_at")
            or existing.get("market_updated_at")
            or existing.get("book_updated_at")
            or existing.get("source_last_update")
        )

        incoming_time = (
            row.get("last_change_at")
            or row.get("market_updated_at")
            or row.get("book_updated_at")
            or row.get("source_last_update")
        )

        if incoming_time is not None and (
            existing_time is None
            or incoming_time >= existing_time
        ):
            deduped[key] = row

    return list(deduped.values())


def upsert_current_markets(conn, rows):
    if not rows:
        return 0

    unique_rows = dedupe_market_rows(rows)

    values = [
        (
            row["provider"],
            row["event_id"],
            row["sport_key"],
            row["bookmaker_key"],
            row["bookmaker_title"],
            row["market_key"],
            row["market_description"],
            row["period"] or "",
            row["player_name"] or "",
            row["outcome_name"],
            row["line"],
            row["odds"],
            row["dfs_odds_type"],
            row["payout_multiplier"],
            row["book_updated_at"],
            row["market_updated_at"],
            row["last_change_at"],
            row["source_last_update"],
        )
        for row in unique_rows
    ]

    sql = """
        INSERT INTO provider_markets (
            provider,
            provider_event_id,
            sport_key,
            bookmaker_key,
            bookmaker_title,
            market_key,
            market_description,
            period,
            player_name,
            outcome_name,
            line,
            odds,
            dfs_odds_type,
            payout_multiplier,
            book_updated_at,
            market_updated_at,
            last_change_at,
            source_last_update
        )
        VALUES %s
        ON CONFLICT (
            provider,
            provider_event_id,
            bookmaker_key,
            market_key,
            period,
            player_name,
            outcome_name,
            line_key
        )
        DO UPDATE SET
            bookmaker_title = EXCLUDED.bookmaker_title,
            market_description = EXCLUDED.market_description,
            odds = EXCLUDED.odds,
            dfs_odds_type = EXCLUDED.dfs_odds_type,
            payout_multiplier = EXCLUDED.payout_multiplier,
            book_updated_at = EXCLUDED.book_updated_at,
            market_updated_at = EXCLUDED.market_updated_at,
            last_change_at = EXCLUDED.last_change_at,
            source_last_update = EXCLUDED.source_last_update,
            last_seen_at = NOW(),
            updated_at = NOW()
    """

    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            values,
            page_size=1000
        )

    duplicate_count = len(rows) - len(unique_rows)

    if duplicate_count:
        print(
            f"Deduplicated market rows: "
            f"{duplicate_count}"
        )

    return len(unique_rows)



def save_market_checkpoint(conn, rows, checkpoint):
    """
    Save a permanent OPEN or CLOSE checkpoint.

    OPEN:
        Insert only the first observation. Existing rows are never changed.

    CLOSE:
        Upsert the latest pregame observation until the event starts.
    """
    if not rows:
        return 0

    checkpoint = str(checkpoint or "").strip().lower()

    if checkpoint not in {"open", "close"}:
        raise ValueError("checkpoint must be 'open' or 'close'")

    unique_rows = dedupe_market_rows(rows)

    values = [
        (
            row["provider"],
            row["event_id"],
            row["sport_key"],
            row["bookmaker_key"],
            row["bookmaker_title"],
            row["market_key"],
            row["market_description"],
            row["period"] or "",
            row["player_name"] or "",
            row["outcome_name"],
            row["line"],
            row["odds"],
            row["dfs_odds_type"],
            row["payout_multiplier"],
            checkpoint,
            row["source_last_update"],
        )
        for row in unique_rows
    ]

    if checkpoint == "open":
        conflict_action = "DO NOTHING"
    else:
        conflict_action = """
        DO UPDATE SET
            bookmaker_title = EXCLUDED.bookmaker_title,
            market_description = EXCLUDED.market_description,
            odds = EXCLUDED.odds,
            dfs_odds_type = EXCLUDED.dfs_odds_type,
            payout_multiplier = EXCLUDED.payout_multiplier,
            source_last_update = EXCLUDED.source_last_update,
            captured_at = NOW()
        """

    sql = f"""
        INSERT INTO provider_market_history (
            provider,
            provider_event_id,
            sport_key,
            bookmaker_key,
            bookmaker_title,
            market_key,
            market_description,
            period,
            player_name,
            outcome_name,
            line,
            odds,
            dfs_odds_type,
            payout_multiplier,
            checkpoint,
            source_last_update
        )
        VALUES %s
        ON CONFLICT (
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
        {conflict_action}
    """

    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            values,
            page_size=1000
        )

    duplicate_count = len(rows) - len(unique_rows)

    if duplicate_count:
        print(
            f"Deduplicated {checkpoint} checkpoint rows: "
            f"{duplicate_count}"
        )

    return len(unique_rows)


def should_capture_close(event, close_window_minutes=60):
    """
    Capture/update CLOSE only during the final pregame window.

    The scheduler may refresh repeatedly in this window. The unique key keeps
    one close row per selection while the upsert replaces it with the newest
    pregame price.
    """
    if not event:
        return False

    if bool(event.get("live")):
        return False

    commence_time = parse_timestamp(event.get("commence_time"))

    if commence_time is None:
        return False

    now_utc = datetime.now(timezone.utc)

    if commence_time <= now_utc:
        return False

    seconds_until_start = (
        commence_time - now_utc
    ).total_seconds()

    return seconds_until_start <= close_window_minutes * 60



def flatten_results(payload):
    event_id = str(payload.get("id"))
    sport_key = payload.get("sport_key")
    rows = []

    for bookmaker in payload.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            for outcome in market.get("outcomes") or []:
                rows.append((
                    "prop_line",
                    event_id,
                    sport_key,
                    bookmaker.get("key"),
                    bookmaker.get("title"),
                    market.get("key"),
                    market.get("description"),
                    outcome.get("description"),
                    str(outcome.get("name") or ""),
                    number_or_none(outcome.get("point")),
                    outcome.get("price"),
                    outcome.get("resolution"),
                    number_or_none(outcome.get("actual_value")),
                    parse_timestamp(outcome.get("resolved_at")),
                    bool(outcome.get("redacted")),
                    outcome.get("dfs_odds_type"),
                    Json(outcome),
                ))

    return rows


def upsert_results(conn, rows):
    if not rows:
        return 0

    # Prop-Line can return the same resolved selection more than once
    # within a payload. PostgreSQL cannot update the same conflict key
    # twice in a single INSERT, so keep one row per unique selection.
    deduped = {}

    for row in rows:
        key = (
            row[0],  # provider
            row[1],  # provider_event_id
            row[3],  # bookmaker_key
            row[5],  # market_key
            row[7] or "",  # player_name
            row[8],  # outcome_name
            row[9] if row[9] is not None else Decimal("-999999999"),
        )

        existing = deduped.get(key)

        if existing is None:
            deduped[key] = row
            continue

        # Prefer the row with the newest resolved_at timestamp.
        existing_resolved_at = existing[13]
        incoming_resolved_at = row[13]

        if (
            incoming_resolved_at is not None
            and (
                existing_resolved_at is None
                or incoming_resolved_at >= existing_resolved_at
            )
        ):
            deduped[key] = row

    unique_rows = list(deduped.values())

    sql = """
        INSERT INTO provider_results (
            provider,
            provider_event_id,
            sport_key,
            bookmaker_key,
            bookmaker_title,
            market_key,
            market_description,
            player_name,
            outcome_name,
            line,
            odds,
            resolution,
            actual_value,
            resolved_at,
            redacted,
            dfs_odds_type,
            raw_outcome
        )
        VALUES %s
        ON CONFLICT (
            provider,
            provider_event_id,
            bookmaker_key,
            market_key,
            player_name,
            outcome_name,
            line_key
        )
        DO UPDATE SET
            bookmaker_title = EXCLUDED.bookmaker_title,
            market_description = EXCLUDED.market_description,
            odds = EXCLUDED.odds,
            resolution = EXCLUDED.resolution,
            actual_value = EXCLUDED.actual_value,
            resolved_at = EXCLUDED.resolved_at,
            redacted = EXCLUDED.redacted,
            dfs_odds_type = EXCLUDED.dfs_odds_type,
            raw_outcome = EXCLUDED.raw_outcome,
            updated_at = NOW()
    """

    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            unique_rows,
            page_size=1000
        )

    duplicate_count = len(rows) - len(unique_rows)

    if duplicate_count:
        print(
            f"Deduplicated result rows: "
            f"{duplicate_count}"
        )

    return len(unique_rows)



def flatten_stats(payload):
    event_id = str(payload.get("id"))
    sport_key = payload.get("sport_key")
    rows = []

    for stat in payload.get("stats") or []:
        rows.append((
            "prop_line",
            event_id,
            sport_key,
            str(stat.get("player_name") or ""),
            str(stat.get("team_abbr") or ""),
            str(stat.get("stat_type") or ""),
            number_or_none(stat.get("stat_value")),
            Json(stat),
        ))

    return rows


def upsert_stats(conn, rows):
    if not rows:
        return 0

    sql = """
        INSERT INTO provider_stats (
            provider,
            provider_event_id,
            sport_key,
            player_name,
            team_abbr,
            stat_type,
            stat_value,
            raw_stat
        )
        VALUES %s
        ON CONFLICT (
            provider,
            provider_event_id,
            player_name,
            team_abbr,
            stat_type
        )
        DO UPDATE SET
            stat_value = EXCLUDED.stat_value,
            raw_stat = EXCLUDED.raw_stat,
            updated_at = NOW()
    """

    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)

    return len(rows)


def select_event_ids(events, event_id, max_events):
    if event_id:
        return [str(event_id)]

    candidates = [
        str(event.get("id"))
        for event in events
        if event.get("id") is not None
    ]

    return candidates[:max_events]



def american_to_implied_probability(odds):
    if odds is None:
        return None

    odds = float(odds)

    if odds > 0:
        return 100.0 / (odds + 100.0)

    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)

    return None


def implied_probability_to_american(probability):
    if probability is None:
        return None

    probability = float(probability)

    if probability <= 0 or probability >= 1:
        return None

    if probability >= 0.5:
        return int(round(
            -100.0 * probability / (1.0 - probability)
        ))

    return int(round(
        100.0 * (1.0 - probability) / probability
    ))


def refresh_market_summary(conn, event_id):
    """
    Rebuild one event's market summary from provider_markets.

    Average odds are calculated by averaging implied probabilities,
    then converting the average back to American odds.
    """
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM provider_market_summary
            WHERE provider = 'prop_line'
              AND provider_event_id = %s
        """, (str(event_id),))

        cur.execute("""
            SELECT
                provider,
                provider_event_id,
                sport_key,
                market_key,
                period,
                player_name,
                outcome_name,
                line,
                bookmaker_key,
                bookmaker_title,
                odds
            FROM provider_markets
            WHERE provider = 'prop_line'
              AND provider_event_id = %s
              AND odds IS NOT NULL
        """, (str(event_id),))

        rows = cur.fetchall()

    grouped = {}

    for row in rows:
        (
            provider,
            provider_event_id,
            sport_key,
            market_key,
            period,
            player_name,
            outcome_name,
            line,
            bookmaker_key,
            bookmaker_title,
            odds,
        ) = row

        key = (
            provider,
            provider_event_id,
            sport_key,
            market_key,
            period or "",
            player_name or "",
            outcome_name,
            line,
        )

        grouped.setdefault(key, []).append({
            "bookmaker_key": bookmaker_key,
            "bookmaker_title": bookmaker_title,
            "odds": int(odds),
        })

    summary_rows = []

    for key, selections in grouped.items():
        valid = [
            selection
            for selection in selections
            if american_to_implied_probability(
                selection["odds"]
            ) is not None
        ]

        if not valid:
            continue

        best = max(valid, key=lambda item: item["odds"])
        worst = min(valid, key=lambda item: item["odds"])

        probabilities = [
            american_to_implied_probability(item["odds"])
            for item in valid
        ]

        average_probability = (
            sum(probabilities) / len(probabilities)
        )

        average_odds = implied_probability_to_american(
            average_probability
        )

        summary_rows.append((
            *key,
            len(valid),
            best["odds"],
            best["bookmaker_key"],
            best["bookmaker_title"],
            worst["odds"],
            Decimal(str(average_probability)),
            average_odds,
        ))

    if not summary_rows:
        return 0

    sql = """
        INSERT INTO provider_market_summary (
            provider,
            provider_event_id,
            sport_key,
            market_key,
            period,
            player_name,
            outcome_name,
            line,
            books_available,
            best_odds,
            best_bookmaker_key,
            best_bookmaker_title,
            worst_odds,
            average_implied_probability,
            average_odds
        )
        VALUES %s
    """

    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            summary_rows,
            page_size=1000
        )

    return len(summary_rows)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Prop-Line data into HitRateHub PostgreSQL."
    )

    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument("--event-id")
    parser.add_argument("--max-events", type=int, default=1)
    parser.add_argument("--markets", nargs="*", default=DEFAULT_MARKETS)

    parser.add_argument("--events", action="store_true")
    parser.add_argument("--odds", action="store_true")
    parser.add_argument("--results", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--all", action="store_true")

    args = parser.parse_args()

    run_events = args.events or args.all
    run_odds = args.odds or args.all
    run_results = args.results or args.all
    run_stats = args.stats or args.all

    if not any((run_events, run_odds, run_results, run_stats)):
        parser.error(
            "Choose --events, --odds, --results, --stats, or --all."
        )

    with PropLineClient() as client:
        conn = get_conn()

        try:
            events = client.get_events(args.sport)

            event_count = upsert_events(conn, events)
            conn.commit()

            print(f"Events cached: {event_count}")

            event_ids = select_event_ids(
                events,
                args.event_id,
                max(1, args.max_events),
            )

            for event_id in event_ids:
                print(f"\nEvent {event_id}")

                if run_odds:
                    odds_payload = client.get_event_odds(
                        args.sport,
                        event_id,
                        markets=args.markets,
                    )

                    market_rows = flatten_markets(odds_payload)
                    current_count = upsert_current_markets(
                        conn,
                        market_rows,
                    )

                    open_count = save_market_checkpoint(
                        conn,
                        market_rows,
                        "open",
                    )

                    event_record = next(
                        (
                            event
                            for event in events
                            if str(event.get("id")) == str(event_id)
                        ),
                        odds_payload,
                    )

                    close_count = 0

                    if should_capture_close(event_record):
                        close_count = save_market_checkpoint(
                            conn,
                            market_rows,
                            "close",
                        )

                    summary_count = refresh_market_summary(
                        conn,
                        event_id,
                    )

                    conn.commit()

                    print(f"Current market rows: {current_count}")
                    print(f"Market summary rows: {summary_count}")
                    print(f"Open checkpoint rows processed: {open_count}")

                    if close_count:
                        print(
                            f"Close checkpoint rows processed: "
                            f"{close_count}"
                        )
                    else:
                        print(
                            "Close checkpoint: skipped "
                            "(outside final pregame window)"
                        )

                if run_results:
                    results_payload = client.get_results(
                        args.sport,
                        event_id,
                        markets=args.markets,
                    )

                    result_rows = flatten_results(results_payload)
                    result_count = upsert_results(conn, result_rows)
                    conn.commit()

                    print(f"Result rows: {result_count}")
                    print(
                        f"Event status: "
                        f"{results_payload.get('status')}"
                    )

                    upsert_events(conn, [results_payload])
                    conn.commit()

                if run_stats:
                    stats_payload = client.get_stats(
                        args.sport,
                        event_id,
                    )

                    stat_rows = flatten_stats(stats_payload)
                    stat_count = upsert_stats(conn, stat_rows)
                    conn.commit()

                    print(f"Stat rows: {stat_count}")

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    print("\nSync completed successfully.")


if __name__ == "__main__":
    main()
