"""Rebuild HitRateHub Edge Finder cache outside Render web requests.

Run from project root:
    python -m scripts.rebuild_edge_finder

Heavy work belongs on PythonAnywhere/cron. Render only reads edge_finder_cache.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import get_conn  # noqa: E402


# Cache prop key -> provider market key -> MLB gamelog stat column
MARKETS = {
    "HR": ("batter_home_runs", "hr"),
    "HITS": ("batter_hits", "h"),
    "TB": ("batter_total_bases", "tb"),
    "RBI": ("batter_rbis", "rbi"),
    "RUNS": ("batter_runs", "runs_scored"),
    "H+R+RBI": ("batter_hits_runs_rbis", "h+runs_scored+rbi"),
    "WALKS": ("batter_walks", "bb"),
    "SINGLES": ("batter_singles", "single"),
    "DOUBLES": ("batter_doubles", "double"),
    "SB": ("batter_stolen_bases", "sb"),
}

WINDOWS = (10, 20, 30, 45)


def edge_label(edge):
    if edge >= 10:
        return "🔥 Heavy Overpriced"
    if edge >= 5:
        return "✅ Overpriced"
    if edge <= -10:
        return "❌ Heavy Underpriced"
    if edge <= -5:
        return "⚠️ Underpriced"
    return "⚖️ Fair Price"


def load_history(conn, snapshot_date):
    return pd.read_sql_query(
        """
        SELECT
            batter_name,
            game_date,
            MAX(team) AS team,
            SUM(COALESCE(h, 0)) AS h,
            SUM(COALESCE(single, 0)) AS single,
            SUM(COALESCE(double, 0)) AS double,
            SUM(COALESCE(tb, 0)) AS tb,
            SUM(COALESCE(hr, 0)) AS hr,
            SUM(COALESCE(bb, 0)) AS bb,
            SUM(COALESCE(sb, 0)) AS sb,
            SUM(COALESCE(runs_scored, 0)) AS runs_scored,
            SUM(COALESCE(rbi, 0)) AS rbi
        FROM mlb_pa_gamelog
        WHERE batter_name IS NOT NULL
          AND game_date IS NOT NULL
          AND game_date < %s::date
          AND game_date >= %s::date - INTERVAL '220 days'
        GROUP BY batter_name, game_date
        ORDER BY batter_name, game_date DESC
        """,
        conn,
        params=[snapshot_date, snapshot_date],
    )


def load_current_consensus(conn, snapshot_date):
    market_keys = [market_key for market_key, _ in MARKETS.values()]
    placeholders = ",".join(["%s"] * len(market_keys))

    sql = f"""
        SELECT
            COALESCE(
                NULLIF(TRIM(ppa.normalized_name), ''),
                TRIM(pms.player_name)
            ) AS player,
            pms.market_key,
            pms.line,
            pms.average_odds AS odds,
            (pms.average_implied_probability * 100.0) AS implied_prob,
            pms.books_available AS book_count
        FROM provider_market_summary pms
        JOIN provider_events pe
          ON pe.provider = pms.provider
         AND pe.provider_event_id = pms.provider_event_id
        LEFT JOIN provider_player_aliases ppa
          ON ppa.provider = pms.provider
         AND ppa.sport_key = pms.sport_key
         AND ppa.raw_player_name = pms.player_name
        WHERE pms.provider = 'prop_line'
          AND pms.sport_key = 'baseball_mlb'
          AND pe.sport_key = 'baseball_mlb'
          AND pms.market_key IN ({placeholders})
          AND LOWER(TRIM(pms.outcome_name)) = 'over'
          AND pms.line IS NOT NULL
          AND pms.average_odds IS NOT NULL
          AND pms.average_implied_probability IS NOT NULL
          AND (
                pe.commence_time AT TIME ZONE 'America/Toronto'
              )::date = %s::date
        ORDER BY player, pms.market_key, pms.line
    """

    return pd.read_sql_query(
        sql,
        conn,
        params=market_keys + [snapshot_date],
    )


def build_rows(history, consensus, snapshot_date):
    if history.empty or consensus.empty:
        return []

    history = history.copy()
    history["game_date"] = pd.to_datetime(history["game_date"], errors="coerce")
    history = history.dropna(subset=["game_date"])

    market_by_provider_key = {
        provider_key: (cache_prop, stat_col)
        for cache_prop, (provider_key, stat_col) in MARKETS.items()
    }

    history_groups = {
        str(player).strip(): group.sort_values("game_date", ascending=False)
        for player, group in history.groupby("batter_name", sort=False)
    }

    rows = []

    for _, market_row in consensus.iterrows():
        player = str(market_row.get("player") or "").strip()
        provider_key = str(market_row.get("market_key") or "").strip()

        if not player or provider_key not in market_by_provider_key:
            continue

        cache_prop, stat_col = market_by_provider_key[provider_key]
        games = history_groups.get(player)

        if games is None or games.empty:
            continue

        try:
            line = float(market_row["line"])
            odds = int(market_row["odds"])
            implied = float(market_row["implied_prob"])
            book_count = int(market_row.get("book_count") or 0)
        except (TypeError, ValueError):
            continue

        rates = {}
        edges = {}

        for window in WINDOWS:
            sample = games.head(window)

            if sample.empty:
                rate = 0.0
            else:
                if stat_col == "h+runs_scored+rbi":
                    values = (
                        pd.to_numeric(sample["h"], errors="coerce").fillna(0)
                        + pd.to_numeric(sample["runs_scored"], errors="coerce").fillna(0)
                        + pd.to_numeric(sample["rbi"], errors="coerce").fillna(0)
                    )
                else:
                    values = pd.to_numeric(
                        sample[stat_col],
                        errors="coerce",
                    ).fillna(0)

                rate = float((values > line).mean() * 100.0)

            rates[window] = round(rate, 1)
            edges[window] = round(rate - implied, 1)

        team = ""
        if "team" in games.columns and not games.empty:
            team = str(games.iloc[0].get("team") or "").strip()

        rows.append({
            "player": player,
            "team": team,
            "prop": cache_prop,
            "ou": "Over",
            "line": line,
            "sportsbook": (
                f"AVG {book_count} BOOK"
                f"{'' if book_count == 1 else 'S'}"
            ),
            "odds": odds,
            "implied_prob": round(implied, 2),
            "hit_rate_l10": rates[10],
            "hit_rate_l20": rates[20],
            "hit_rate_l30": rates[30],
            "hit_rate_l45": rates[45],
            "edge_l10": edges[10],
            "edge_l20": edges[20],
            "edge_l30": edges[30],
            "edge_l45": edges[45],
            "label_l20": edge_label(edges[20]),
            "updated_at": datetime.now(ZoneInfo("America/Toronto")),
            "result_stat": None,
            "result_status": None,
            "game_date": snapshot_date,
            "snapshot_date": snapshot_date,
        })

    return rows


def main():
    snapshot_date = datetime.now(ZoneInfo("America/Toronto")).date()
    conn = get_conn()

    try:
        print("=" * 64)
        print("HitRateHub Edge Finder Cache Builder")
        print("=" * 64)
        print(f"Snapshot date: {snapshot_date}")

        history = load_history(conn, snapshot_date)
        consensus = load_current_consensus(conn, snapshot_date)

        print(f"Historical game rows: {len(history):,}")
        print(f"Current consensus rows: {len(consensus):,}")

        rows = build_rows(history, consensus, snapshot_date)

        if not rows:
            print("No Edge Finder rows produced.")
            print("Existing cache was NOT deleted.")
            return 2

        insert_columns = [
            "player",
            "team",
            "prop",
            "ou",
            "line",
            "sportsbook",
            "odds",
            "implied_prob",
            "hit_rate_l10",
            "hit_rate_l20",
            "hit_rate_l30",
            "hit_rate_l45",
            "edge_l10",
            "edge_l20",
            "edge_l30",
            "edge_l45",
            "label_l20",
            "updated_at",
            "result_stat",
            "result_status",
            "game_date",
            "snapshot_date",
        ]

        placeholders = ",".join(["%s"] * len(insert_columns))
        column_sql = ",".join(insert_columns)

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edge_finder_cache WHERE snapshot_date = %s",
                (snapshot_date,),
            )

            insert_sql = (
                f"INSERT INTO edge_finder_cache ({column_sql}) "
                f"VALUES ({placeholders})"
            )

            values = [
                tuple(row[column] for column in insert_columns)
                for row in rows
            ]

            cur.executemany(insert_sql, values)

        conn.commit()

        print(f"Inserted Edge Finder rows: {len(rows):,}")
        print("=" * 64)
        print("Edge Finder rebuild complete")
        print("=" * 64)
        return 0

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
