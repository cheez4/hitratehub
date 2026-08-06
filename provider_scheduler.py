"""
HitRateHub Prop-Line background scheduler.

This service keeps the provider cache current. It does not run the Flask web
server and it does not place or accept wagers.

Required environment variables:
    PROP_LINE_API_KEY
    DATABASE_URL (loaded by database.py)

Local tests:
    python provider_scheduler.py --once
    python provider_scheduler.py --once --max-events 3

Render Background Worker:
    python provider_scheduler.py
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from database import get_conn
from psycopg2.extras import Json
from services.prop_line import (
    PropLineAuthError,
    PropLineClient,
    PropLineRequestError,
)

from sync_prop_line import (
    DEFAULT_MARKETS,
    DEFAULT_SPORT,
    flatten_markets,
    flatten_results,
    flatten_stats,
    refresh_market_summary,
    save_market_checkpoint,
    should_capture_close,
    upsert_current_markets,
    upsert_events,
    upsert_results,
    upsert_stats,
)


LOGGER = logging.getLogger("provider_scheduler")

STOP_REQUESTED = False

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_ODDS_HORIZON_HOURS = 36
DEFAULT_RESULTS_LOOKBACK_HOURS = 18
DEFAULT_RESULTS_DELAY_MINUTES = 5


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | %(levelname)s | "
            "%(name)s | %(message)s"
        ),
    )


def request_stop(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("Shutdown requested.")


def parse_api_time(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def event_id(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("provider_event_id") or "")


def event_is_upcoming_for_odds(
    event: dict[str, Any],
    *,
    horizon_hours: int,
) -> bool:
    start = parse_api_time(event.get("commence_time"))

    if start is None:
        return False

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=horizon_hours)

    return (
        not bool(event.get("live"))
        and now < start <= horizon
    )


def event_is_recent_final(
    event: dict[str, Any],
    *,
    lookback_hours: int,
    result_delay_minutes: int,
) -> bool:
    status = str(event.get("status") or "").strip().lower()

    if status not in {"final", "completed", "complete"}:
        return False

    start = parse_api_time(event.get("commence_time"))

    if start is None:
        return True

    now = datetime.now(timezone.utc)

    # Do not repeatedly process very old games.
    if start < now - timedelta(hours=lookback_hours):
        return False

    # MLB duration varies, so scores status is the authority. The small delay
    # gives the provider time to resolve outcome rows after marking the game final.
    updated = parse_api_time(
        (event.get("context") or {}).get("updated_at")
        or event.get("last_update")
    )

    if updated is not None:
        return updated <= now - timedelta(
            minutes=result_delay_minutes
        )

    return True


def load_already_resolved_event_ids(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT provider_event_id
            FROM provider_results
            WHERE provider = 'prop_line'
              AND resolution IS NOT NULL
        """)

        return {
            str(row[0])
            for row in cur.fetchall()
        }


def sync_one_odds_event(
    client: PropLineClient,
    conn,
    event: dict[str, Any],
    *,
    sport: str,
    markets: list[str],
) -> dict[str, int]:
    current_event_id = event_id(event)

    payload = client.get_event_odds(
        sport,
        current_event_id,
        markets=markets,
    )

    rows = flatten_markets(payload)

    current_count = upsert_current_markets(
        conn,
        rows,
    )

    open_count = save_market_checkpoint(
        conn,
        rows,
        "open",
    )

    close_count = 0

    if should_capture_close(event):
        close_count = save_market_checkpoint(
            conn,
            rows,
            "close",
        )

    summary_count = refresh_market_summary(
        conn,
        current_event_id,
    )

    conn.commit()

    return {
        "current": current_count,
        "open": open_count,
        "close": close_count,
        "summary": summary_count,
    }


def sync_one_final_event(
    client: PropLineClient,
    conn,
    event: dict[str, Any],
    *,
    sport: str,
    markets: list[str],
) -> dict[str, int]:
    current_event_id = event_id(event)

    results_payload = client.get_results(
        sport,
        current_event_id,
        markets=markets,
    )

    result_rows = flatten_results(results_payload)
    result_count = upsert_results(conn, result_rows)

    stats_payload = client.get_stats(
        sport,
        current_event_id,
    )

    stat_rows = flatten_stats(stats_payload)
    stat_count = upsert_stats(conn, stat_rows)

    upsert_events(
        conn,
        [results_payload],
    )

    conn.commit()

    return {
        "results": result_count,
        "stats": stat_count,
    }



def create_sync_log(conn, *, sport: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO provider_sync_log (
                provider,
                sport_key,
                run_type,
                status,
                started_at
            )
            VALUES (
                'prop_line',
                %s,
                'scheduler_cycle',
                'running',
                NOW()
            )
            RETURNING id
        """, (sport,))

        sync_log_id = int(cur.fetchone()[0])

    conn.commit()
    return sync_log_id


def finish_sync_log(
    conn,
    *,
    sync_log_id: int,
    totals: dict[str, int],
    started_monotonic: float,
    status: str,
    error_message: str | None = None,
) -> None:
    duration_seconds = round(
        time.monotonic() - started_monotonic,
        3,
    )

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE provider_sync_log
            SET
                status = %s,
                finished_at = NOW(),
                duration_seconds = %s,
                events_cached = %s,
                odds_events = %s,
                market_rows = %s,
                summary_rows = %s,
                open_rows = %s,
                close_rows = %s,
                final_events = %s,
                result_rows = %s,
                stat_rows = %s,
                error_count = %s,
                error_message = %s,
                details = %s
            WHERE id = %s
        """, (
            status,
            duration_seconds,
            totals.get("events", 0),
            totals.get("odds_events", 0),
            totals.get("market_rows", 0),
            totals.get("summary_rows", 0),
            totals.get("open_rows", 0),
            totals.get("close_rows", 0),
            totals.get("final_events", 0),
            totals.get("result_rows", 0),
            totals.get("stat_rows", 0),
            totals.get("errors", 0),
            error_message,
            Json(totals),
            sync_log_id,
        ))

    conn.commit()


def run_cycle(
    *,
    sport: str,
    markets: list[str],
    max_events: int | None,
    odds_horizon_hours: int,
    results_lookback_hours: int,
    results_delay_minutes: int,
) -> dict[str, int]:
    totals = {
        "events": 0,
        "odds_events": 0,
        "market_rows": 0,
        "summary_rows": 0,
        "open_rows": 0,
        "close_rows": 0,
        "final_events": 0,
        "result_rows": 0,
        "stat_rows": 0,
        "errors": 0,
    }

    with PropLineClient() as client:
        conn = get_conn()
        sync_log_id = None
        cycle_started = time.monotonic()

        try:
            sync_log_id = create_sync_log(
                conn,
                sport=sport,
            )

            events = client.get_events(sport)
            totals["events"] = upsert_events(conn, events)
            conn.commit()

            upcoming = [
                event
                for event in events
                if event_is_upcoming_for_odds(
                    event,
                    horizon_hours=odds_horizon_hours,
                )
            ]

            upcoming.sort(
                key=lambda item: (
                    parse_api_time(item.get("commence_time"))
                    or datetime.max.replace(tzinfo=timezone.utc)
                )
            )

            if max_events is not None:
                upcoming = upcoming[:max_events]

            LOGGER.info(
                "Events cached=%s, upcoming odds events=%s",
                totals["events"],
                len(upcoming),
            )

            for event in upcoming:
                current_event_id = event_id(event)

                try:
                    counts = sync_one_odds_event(
                        client,
                        conn,
                        event,
                        sport=sport,
                        markets=markets,
                    )

                    totals["odds_events"] += 1
                    totals["market_rows"] += counts["current"]
                    totals["summary_rows"] += counts["summary"]
                    totals["open_rows"] += counts["open"]
                    totals["close_rows"] += counts["close"]

                    LOGGER.info(
                        "Odds event=%s current=%s summary=%s "
                        "open=%s close=%s",
                        current_event_id,
                        counts["current"],
                        counts["summary"],
                        counts["open"],
                        counts["close"],
                    )

                except Exception:
                    conn.rollback()
                    totals["errors"] += 1
                    LOGGER.exception(
                        "Odds sync failed for event %s",
                        current_event_id,
                    )

            scores = client.get_scores(sport)

            # Scores contain authoritative event status and final scores.
            upsert_events(conn, scores)
            conn.commit()

            resolved_event_ids = load_already_resolved_event_ids(
                conn
            )

            final_events = [
                event
                for event in scores
                if (
                    event_id(event) not in resolved_event_ids
                    and event_is_recent_final(
                        event,
                        lookback_hours=results_lookback_hours,
                        result_delay_minutes=results_delay_minutes,
                    )
                )
            ]

            final_events.sort(
                key=lambda item: (
                    parse_api_time(item.get("commence_time"))
                    or datetime.min.replace(tzinfo=timezone.utc)
                )
            )

            if max_events is not None:
                final_events = final_events[:max_events]

            LOGGER.info(
                "Final events awaiting results=%s",
                len(final_events),
            )

            for event in final_events:
                current_event_id = event_id(event)

                try:
                    counts = sync_one_final_event(
                        client,
                        conn,
                        event,
                        sport=sport,
                        markets=markets,
                    )

                    totals["final_events"] += 1
                    totals["result_rows"] += counts["results"]
                    totals["stat_rows"] += counts["stats"]

                    LOGGER.info(
                        "Final event=%s results=%s stats=%s",
                        current_event_id,
                        counts["results"],
                        counts["stats"],
                    )

                except Exception:
                    conn.rollback()
                    totals["errors"] += 1
                    LOGGER.exception(
                        "Final sync failed for event %s",
                        current_event_id,
                    )

            log_status = (
                "success"
                if totals["errors"] == 0
                else "partial"
            )

            if sync_log_id is not None:
                finish_sync_log(
                    conn,
                    sync_log_id=sync_log_id,
                    totals=totals,
                    started_monotonic=cycle_started,
                    status=log_status,
                )

        except Exception as exc:
            conn.rollback()

            if sync_log_id is not None:
                try:
                    finish_sync_log(
                        conn,
                        sync_log_id=sync_log_id,
                        totals=totals,
                        started_monotonic=cycle_started,
                        status="failed",
                        error_message=(
                            f"{type(exc).__name__}: {exc}"
                        )[:4000],
                    )
                except Exception:
                    conn.rollback()
                    LOGGER.exception(
                        "Failed to write scheduler failure log."
                    )

            raise

        finally:
            conn.close()

    return totals


def main() -> int:
    configure_logging()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    parser = argparse.ArgumentParser(
        description="Run the HitRateHub provider scheduler."
    )

    parser.add_argument(
        "--sport",
        default=DEFAULT_SPORT,
    )

    parser.add_argument(
        "--markets",
        nargs="*",
        default=DEFAULT_MARKETS,
    )

    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
    )

    parser.add_argument(
        "--odds-horizon-hours",
        type=int,
        default=DEFAULT_ODDS_HORIZON_HOURS,
    )

    parser.add_argument(
        "--results-lookback-hours",
        type=int,
        default=DEFAULT_RESULTS_LOOKBACK_HOURS,
    )

    parser.add_argument(
        "--results-delay-minutes",
        type=int,
        default=DEFAULT_RESULTS_DELAY_MINUTES,
    )

    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--once",
        action="store_true",
    )

    args = parser.parse_args()

    LOGGER.info(
        "Scheduler starting sport=%s interval=%ss",
        args.sport,
        args.interval_seconds,
    )

    while not STOP_REQUESTED:
        cycle_started = time.monotonic()

        try:
            totals = run_cycle(
                sport=args.sport,
                markets=args.markets,
                max_events=args.max_events,
                odds_horizon_hours=args.odds_horizon_hours,
                results_lookback_hours=args.results_lookback_hours,
                results_delay_minutes=args.results_delay_minutes,
            )

            LOGGER.info(
                "Cycle complete: %s",
                totals,
            )

        except PropLineAuthError:
            LOGGER.exception(
                "Provider authentication failed. "
                "Scheduler is stopping."
            )
            return 1

        except PropLineRequestError:
            LOGGER.exception(
                "Provider request failed."
            )

        except Exception:
            LOGGER.exception(
                "Unexpected scheduler cycle failure."
            )

        if args.once:
            break

        elapsed = time.monotonic() - cycle_started
        sleep_seconds = max(
            1,
            args.interval_seconds - int(elapsed),
        )

        LOGGER.info(
            "Sleeping %s seconds.",
            sleep_seconds,
        )

        for _ in range(sleep_seconds):
            if STOP_REQUESTED:
                break

            time.sleep(1)

    LOGGER.info("Scheduler stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
