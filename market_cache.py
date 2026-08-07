from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from psycopg2.extras import Json, execute_values


PLAYER_SUFFIXES = (
    "strikeouts thrown", "pitching strikeouts",
    "pitcher strikeouts", "outs recorded",
    "pitcher outs", "hits allowed", "walks allowed",
    "earned runs allowed", "runs allowed",
    "home runs allowed", "total bases", "home runs",
    "runs batted in", "rbis", "hits", "runs",
)

MARKET_LABELS = {
    "batter_hits": "Hits",
    "batter_home_runs": "Home Runs",
    "batter_total_bases": "Total Bases",
    "batter_rbis": "RBI",
    "batter_runs": "Runs",
    "pitcher_strikeouts": "Strikeouts",
    "pitcher_outs": "Outs Recorded",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks": "Walks",
    "pitcher_earned_runs": "Earned Runs",
    "pitcher_runs_allowed": "Runs Allowed",
}


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_player_name(value: Any) -> str:
    name = clean_text(value)

    if not name:
        return ""

    name = re.sub(
        r"\s*\([A-Z0-9]{2,4}\)\s*$",
        "",
        name,
    ).strip()

    lowered = name.lower()

    for suffix in PLAYER_SUFFIXES:
        marker = " " + suffix

        if lowered.endswith(marker):
            name = name[:-len(marker)].strip()
            break

    return re.sub(r"\s+", " ", name).strip()


def market_label(value: Any) -> str:
    key = clean_text(value)
    return MARKET_LABELS.get(
        key,
        key.replace("_", " ").title(),
    )


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main_score(item: dict[str, Any]) -> tuple:
    average_odds = item.get("average_odds")
    distance = (
        abs(int(average_odds) + 110)
        if average_odds is not None
        else 999999
    )

    return (
        int(item.get("books_available") or 0),
        -distance,
    )


def refresh_market_cache(
    conn,
    event_ids: Iterable[str] | None = None,
) -> int:
    event_ids = [
        str(value)
        for value in (event_ids or [])
        if str(value).strip()
    ]

    summary_filter = ""
    summary_params = []

    if event_ids:
        marks = ",".join(["%s"] * len(event_ids))
        summary_filter = (
            f" AND pms.provider_event_id IN ({marks})"
        )
        summary_params = event_ids

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                pms.id AS summary_id,
                pms.provider,
                pms.provider_event_id,
                pms.sport_key,
                pe.home_team,
                pe.away_team,
                pe.commence_time,
                pms.market_key,
                pms.period,
                pms.player_name,
                pms.outcome_name,
                pms.line,
                pms.books_available,
                pms.best_odds,
                pms.best_bookmaker_key,
                pms.best_bookmaker_title,
                pms.worst_odds,
                pms.average_odds,
                pms.last_updated
            FROM provider_market_summary pms
            JOIN provider_events pe
              ON pe.provider = pms.provider
             AND pe.provider_event_id =
                 pms.provider_event_id
            WHERE pms.provider = 'prop_line'
              AND pms.player_name <> ''
              {summary_filter}
        """, summary_params)

        names = [col.name for col in cur.description]
        summaries = [
            dict(zip(names, row))
            for row in cur.fetchall()
        ]

    market_filter = ""
    market_params = []

    if event_ids:
        marks = ",".join(["%s"] * len(event_ids))
        market_filter = (
            f" AND provider_event_id IN ({marks})"
        )
        market_params = event_ids

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                id AS provider_market_id,
                provider_event_id,
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
              AND player_name <> ''
              AND odds IS NOT NULL
              {market_filter}
            ORDER BY odds DESC
        """, market_params)

        names = [col.name for col in cur.description]
        market_rows = [
            dict(zip(names, row))
            for row in cur.fetchall()
        ]

    books_by_selection = defaultdict(list)

    for row in market_rows:
        key = (
            clean_text(row["provider_event_id"]),
            clean_text(row["market_key"]),
            clean_text(row["period"]),
            clean_text(row["player_name"]),
            clean_text(row["outcome_name"]).lower(),
            number_or_none(row["line"]),
        )

        books_by_selection[key].append({
            "provider_market_id": int(
                row["provider_market_id"]
            ),
            "bookmaker_key": clean_text(
                row["bookmaker_key"]
            ),
            "bookmaker_title": clean_text(
                row["bookmaker_title"]
            ),
            "odds": int(row["odds"]),
        })

    deduped = {}

    for row in summaries:
        clean_player = normalize_player_name(
            row["player_name"]
        )

        if not clean_player:
            continue

        line = number_or_none(row["line"])
        raw_player = clean_text(row["player_name"])

        cache_key = (
            clean_text(row["provider_event_id"]),
            clean_player.lower(),
            clean_text(row["market_key"]),
            clean_text(row["period"]),
            clean_text(row["outcome_name"]).lower(),
            line,
        )

        raw_key = (
            clean_text(row["provider_event_id"]),
            clean_text(row["market_key"]),
            clean_text(row["period"]),
            raw_player,
            clean_text(row["outcome_name"]).lower(),
            line,
        )

        candidate = dict(row)
        candidate.update({
            "clean_player_name": clean_player,
            "raw_player_name": raw_player,
            "market_label": market_label(
                row["market_key"]
            ),
            "line": line,
            "books": books_by_selection.get(
                raw_key,
                [],
            ),
        })

        current = deduped.get(cache_key)

        if (
            current is None
            or int(candidate["books_available"] or 0)
            > int(current["books_available"] or 0)
        ):
            deduped[cache_key] = candidate

    grouped = defaultdict(list)

    for item in deduped.values():
        grouped[(
            item["provider_event_id"],
            item["clean_player_name"].lower(),
            item["market_key"],
            item["outcome_name"].lower(),
        )].append(item)

    for items in grouped.values():
        main = max(items, key=main_score)

        for item in items:
            item["is_main"] = item is main

    values = []

    for item in deduped.values():
        values.append((
            item["provider"],
            item["provider_event_id"],
            item["sport_key"],
            item["home_team"],
            item["away_team"],
            item["commence_time"],
            item["clean_player_name"],
            item["raw_player_name"],
            item["market_key"],
            item["market_label"],
            item["period"] or "",
            item["outcome_name"],
            item["line"],
            int(item["summary_id"]),
            int(item["books_available"] or 0),
            item["best_odds"],
            item["best_bookmaker_key"],
            item["best_bookmaker_title"],
            item["worst_odds"],
            item["average_odds"],
            bool(item["is_main"]),
            Json(item["books"]),
            item["last_updated"],
        ))

    with conn.cursor() as cur:
        if event_ids:
            cur.execute("""
                DELETE FROM provider_market_cache
                WHERE provider = 'prop_line'
                  AND provider_event_id = ANY(%s)
            """, (event_ids,))
        else:
            cur.execute("""
                DELETE FROM provider_market_cache
                WHERE provider = 'prop_line'
            """)

        if values:
            execute_values(cur, """
                INSERT INTO provider_market_cache (
                    provider, provider_event_id, sport_key,
                    home_team, away_team, commence_time,
                    clean_player_name, raw_player_name,
                    market_key, market_label, period,
                    outcome_name, line, summary_id,
                    books_available, best_odds,
                    best_bookmaker_key,
                    best_bookmaker_title, worst_odds,
                    average_odds, is_main, books,
                    source_updated_at, cache_updated_at
                )
                VALUES %s
            """, values, page_size=1000)

    return len(values)
