from flask import Flask, render_template, request, jsonify
from flask_caching import Cache
import unicodedata
import pandas as pd
import psycopg2
import os
import uuid
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

app = Flask(__name__)

cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300
})

MAX_COMPARE_PLAYERS = 10

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def read_sql(query, params=None):
    conn = get_conn()
    try:
        return pd.read_sql(query, conn, params=params)
    finally:
        conn.close()


def safe_int(value, default=10):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.5):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def clean_text(value):
    return "" if value is None else str(value).strip()


def normalize_name(name):
    if not name:
        return ""
    return unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower().strip()


def get_compare_values():
    values = {}

    for i in range(1, MAX_COMPARE_PLAYERS + 1):
        field_name = "calc_player" if i == 1 else f"calc_compare_{i}"
        values[field_name] = clean_text(request.args.get(field_name, ""))

    return values


def get_compare_players_from_request():
    players = []

    for i in range(1, MAX_COMPARE_PLAYERS + 1):
        field_name = "calc_player" if i == 1 else f"calc_compare_{i}"
        player_name = clean_text(request.args.get(field_name, ""))

        if player_name and player_name not in players:
            players.append(player_name)

    return players

def calculate_streaks(results):
    if not results:
        return {
            "current_streak_type": "-",
            "current_streak_count": 0,
            "best_hit_streak": 0,
            "best_miss_streak": 0,
        }

    first = results[0]
    current_count = 0

    for r in results:
        if r == first:
            current_count += 1
        else:
            break

    best_hit = 0
    best_miss = 0
    cur_hit = 0
    cur_miss = 0

    for r in results:
        if r:
            cur_hit += 1
            cur_miss = 0
        else:
            cur_miss += 1
            cur_hit = 0

        best_hit = max(best_hit, cur_hit)
        best_miss = max(best_miss, cur_miss)

    return {
        "current_streak_type": "hit" if first else "miss",
        "current_streak_count": current_count,
        "best_hit_streak": best_hit,
        "best_miss_streak": best_miss,
    }

@cache.cached(timeout=300, key_prefix="pa_data")
def get_pa_data():
    return read_sql("""
        SELECT
            batter_name,
            team,
            opp_team,
            pitcher_hand,
            game_date,
            COALESCE(h, 0) AS h,
            COALESCE(tb, 0) AS tb,
            COALESCE(hr, 0) AS hr,
            COALESCE(runs_scored, 0) AS runs_scored,
            COALESCE(rbi, 0) AS rbi
        FROM mlb_pa_gamelog
        WHERE batter_name IS NOT NULL
          AND game_date IS NOT NULL
    """)


@cache.cached(timeout=300, key_prefix="pitcher_data")
def get_pitcher_data():
    return read_sql("""
        SELECT
            id,
            player_id,
            player_name,
            season,
            game_date,
            game_id,
            is_home,
            COALESCE(strikeouts, 0) AS strikeouts,
            COALESCE(runs_allowed, 0) AS runs_allowed,
            COALESCE(earned_runs, 0) AS earned_runs,
            COALESCE(walks_allowed, 0) AS walks_allowed,
            COALESCE(hits_allowed, 0) AS hits_allowed,
            COALESCE(home_runs_allowed, 0) AS home_runs_allowed,
            COALESCE(innings_pitched, 0) AS innings_pitched,
            COALESCE(batters_faced, 0) AS batters_faced
        FROM mlb_pitcher_gamelogs
        WHERE player_name IS NOT NULL
          AND game_date IS NOT NULL
    """)


@cache.cached(timeout=300, key_prefix="hitter_names")
def get_hitter_names():
    try:
        df = read_sql("""
            SELECT DISTINCT batter_name
            FROM mlb_pa_gamelog
            WHERE batter_name IS NOT NULL
            ORDER BY batter_name
        """)
        return df["batter_name"].dropna().astype(str).tolist()
    except Exception:
        return []


@cache.cached(timeout=300, key_prefix="pitcher_names")
def get_pitcher_names():
    try:
        df = read_sql("""
            SELECT DISTINCT player_name
            FROM mlb_pitcher_gamelogs
            WHERE player_name IS NOT NULL
            ORDER BY player_name
        """)
        return df["player_name"].dropna().astype(str).tolist()
    except Exception:
        return []


@cache.cached(timeout=300, key_prefix="today_lineups")
def get_today_lineups():
    try:
        df = read_sql("""
            SELECT player_name, batting_order, lineup_status
            FROM mlb_daily_lineups
            WHERE game_date = (
                SELECT MAX(game_date)
                FROM mlb_daily_lineups
            )
        """)

        lineup_map = {}

        for _, row in df.iterrows():
            name = str(row["player_name"]).strip()

            item = {
                "order": row["batting_order"],
                "status": row["lineup_status"]
            }

            lineup_map[name] = item
            lineup_map[name.lower()] = item
            lineup_map[normalize_name(name)] = item

        return lineup_map

    except Exception as e:
        print("Lineup map error:", e)
        return {}


def get_teams_from_pa(df=None):
    try:
        if df is None:
            df = read_sql("""
                SELECT DISTINCT opp_team
                FROM mlb_pa_gamelog
                WHERE opp_team IS NOT NULL
                ORDER BY opp_team
            """)
            return df["opp_team"].dropna().astype(str).tolist()

        return sorted(df["opp_team"].dropna().astype(str).unique().tolist())
    except Exception:
        return []


def calculate_hitter_stat(df, prop):
    if prop == "hits":
        return df["h"]
    if prop == "total_bases":
        return df["tb"]
    if prop == "home_runs":
        return df["hr"]
    if prop == "runs":
        return df["runs_scored"]
    if prop == "rbi":
        return df["rbi"]
    return df["h"]


def calculate_pitcher_stat(df, prop):
    if prop == "strikeouts":
        return pd.to_numeric(df["strikeouts"], errors="coerce").fillna(0)
    return pd.Series([0] * len(df), index=df.index)


def filter_hitter_df(df, vs_team="", vs_hand=""):
    out = df.copy()

    if vs_team:
        out = out[out["opp_team"].astype(str) == vs_team]

    if vs_hand:
        out = out[out["pitcher_hand"].astype(str).str.upper() == vs_hand.upper()]

    return out


def apply_lineup_filter(df, lineup_map, lineup_filter):
    if df.empty or "batter_name" not in df.columns:
        return df

    if lineup_filter == "all":
        return df

    allowed_names = set()

    for name, info in lineup_map.items():
        status = info.get("status")

        if lineup_filter == "confirmed" and status == "confirmed":
            allowed_names.add(name)
            allowed_names.add(normalize_name(name))

        if lineup_filter == "confirmed_probable" and status in ("confirmed", "probable"):
            allowed_names.add(name)
            allowed_names.add(normalize_name(name))

    temp = df.copy()
    temp["_name_key"] = temp["batter_name"].apply(normalize_name)

    return temp[
        temp["batter_name"].isin(allowed_names) |
        temp["batter_name"].str.lower().isin(allowed_names) |
        temp["_name_key"].isin(allowed_names)
    ].drop(columns=["_name_key"])


def filter_text(vs_team="", vs_hand=""):
    parts = []

    if vs_team:
        parts.append(f"Vs {vs_team}")

    if vs_hand == "R":
        parts.append("Vs RHP")
    elif vs_hand == "L":
        parts.append("Vs LHP")

    return " • ".join(parts)


def hit_check(value, mode, line, min_value, max_value):
    if mode == "under":
        return value < line
    if mode == "range":
        return min_value <= value <= max_value
    return value > line


def build_hitter_game_rows(df, player_name, prop, window, mode, line, min_value, max_value):
    player_df = df[df["batter_name"].astype(str) == player_name].copy()

    if player_df.empty:
        return pd.DataFrame(columns=["game_date", "stat_value", "hit"])

    player_df["game_date"] = pd.to_datetime(player_df["game_date"], errors="coerce")
    player_df = player_df.dropna(subset=["game_date"])

    grouped = (
        player_df
        .groupby("game_date", as_index=False)
        .agg({
            "h": "sum",
            "tb": "sum",
            "hr": "sum",
            "runs_scored": "sum",
            "rbi": "sum",
            "team": "last",
            "opp_team": "last"
        })
        .sort_values("game_date", ascending=False)
        .head(window)
    )

    if grouped.empty:
        return grouped

    grouped["stat_value"] = calculate_hitter_stat(grouped, prop)
    grouped["hit"] = grouped["stat_value"].apply(
        lambda v: hit_check(float(v), mode, line, min_value, max_value)
    )
    grouped["game_date"] = grouped["game_date"].dt.strftime("%Y-%m-%d")

    return grouped


def build_pitcher_game_rows(df, player_name, prop, window, mode, line, min_value, max_value):
    pitcher_df = df[df["player_name"].astype(str) == player_name].copy()

    if pitcher_df.empty:
        return pd.DataFrame(columns=["game_date", "stat_value", "hit"])

    pitcher_df["game_date"] = pd.to_datetime(pitcher_df["game_date"], errors="coerce")
    pitcher_df = pitcher_df.dropna(subset=["game_date"])

    pitcher_df = pitcher_df.sort_values("game_date", ascending=False).head(window)
    pitcher_df["stat_value"] = calculate_pitcher_stat(pitcher_df, prop)
    pitcher_df["hit"] = pitcher_df["stat_value"].apply(
        lambda v: hit_check(float(v), mode, line, min_value, max_value)
    )
    pitcher_df["game_date"] = pitcher_df["game_date"].dt.strftime("%Y-%m-%d")

    return pitcher_df[["game_date", "stat_value", "hit"]]


def summarize_player(player_name, rows, prop, window, mode, line, min_value, max_value, ftext):
    games = len(rows)
    hits = int(rows["hit"].sum()) if games and "hit" in rows.columns else 0
    avg = round(float(rows["stat_value"].mean()), 2) if games and "stat_value" in rows.columns else 0

    recent_games = []
    if games:
        for item in rows[["game_date", "stat_value", "hit"]].to_dict("records"):
            recent_games.append({
                "game_date": item["game_date"],
                "stat_value": float(item["stat_value"]) if not pd.isna(item["stat_value"]) else 0,
                "hit": bool(item["hit"])
            })

    results = rows["hit"].tolist() if "hit" in rows.columns else []

    streaks = calculate_streaks(results)

    return {
        "player_name": player_name,
        "prop_type": prop,
        "window": window,
        "calc_mode": mode,
        "line": line,
        "min_value": min_value,
        "max_value": max_value,
        "hit_rate": round((hits / games) * 100, 1) if games else 0,
        "hits": hits,
        "games": games,
        "average": avg,
        "recent_games": recent_games,
        "filter_text": ftext,
        "current_streak_type": streaks["current_streak_type"],
        "current_streak_count": streaks["current_streak_count"],
        "best_hit_streak": streaks["best_hit_streak"],
        "best_miss_streak": streaks["best_miss_streak"],

        "current_odds": None,
        "implied_prob": None,
        "edge_diff": None,
        "edge_label": "⚖️ Fair Price",
    }

def prop_to_odds_prop(prop):
    mapping = {
        "hits": "HITS",
        "total_bases": "TB",
        "home_runs": "HR",
        "runs": "RUNS",
        "rbi": "RBI",
        "strikeouts": "SO"
    }
    return mapping.get(prop, prop.upper())

def american_to_implied_prob(odds):
    try:
        odds = int(odds)
    except Exception:
        return None

    if odds > 0:
        return round((100 / (odds + 100)) * 100, 1)

    return round((abs(odds) / (abs(odds) + 100)) * 100, 1)

def get_closing_odds_lookup(players, prop):
    if not players:
        return {}

    odds_prop = prop_to_odds_prop(prop)

    placeholders = ",".join(["%s"] * len(players))

    query = f"""
        SELECT
            player,
            DATE(starttime) AS game_date,
            odds,
            sportsbook,
            line
        FROM closing_odds
        WHERE player IN ({placeholders})
          AND prop = %s
          AND sportsbook = 'fanduel'
          AND ou = 'over'
          AND ismain = 1
    """

    params = players + [odds_prop]

    df = read_sql(query, params)

    lookup = {}

    for _, row in df.iterrows():
        key = (
            str(row["player"]).strip(),
            str(row["game_date"])
        )

        implied = american_to_implied_prob(row["odds"])

        lookup[key] = {
            "odds": int(row["odds"]),
            "sportsbook": row["sportsbook"],
            "line": float(row["line"]) if row["line"] is not None else None,
            "implied_prob": implied
        }

    return lookup

def build_compare_result(players, role, source_df, prop, window, mode, line, min_value, max_value, ftext):
    summaries = []
    rows_by_player = {}
    odds_lookup = get_closing_odds_lookup(players, prop) if role == "hitter" else {}

    for player_name in players:
        if role == "hitter":
            rows = build_hitter_game_rows(source_df, player_name, prop, window, mode, line, min_value, max_value)
        else:
            rows = build_pitcher_game_rows(source_df, player_name, prop, window, mode, line, min_value, max_value)

        summary = summarize_player(player_name, rows, prop, window, mode, line, min_value, max_value, ftext)

        player_odds = [
            v for (p, d), v in odds_lookup.items()
            if p == player_name
        ]

        if player_odds:
            latest_odds = player_odds[0]
            summary["current_odds"] = latest_odds.get("odds")
            summary["implied_prob"] = latest_odds.get("implied_prob")

            implied_prob = latest_odds.get("implied_prob")

            if implied_prob is not None:
                edge_diff = round(summary["hit_rate"] - implied_prob, 1)
                summary["edge_diff"] = edge_diff

                if edge_diff >= 10:
                    summary["edge_label"] = "🔥 Heavy Overpriced"
                elif edge_diff >= 5:
                    summary["edge_label"] = "✅ Overpriced"
                elif edge_diff <= -10:
                    summary["edge_label"] = "❌ Heavy Underpriced"
                elif edge_diff <= -5:
                    summary["edge_label"] = "⚠️ Underpriced"

        summaries.append(summary)
        rows_by_player[player_name] = rows.set_index("game_date") if not rows.empty else rows

    all_dates = sorted(
        set().union(*[
            set(rows.index.tolist()) for rows in rows_by_player.values() if not rows.empty
        ]),
        reverse=True
    )[:window]

    compare_rows = []

    for game_date in all_dates:
        player_cells = []

        for player_name in players:
            rows = rows_by_player[player_name]

            if rows.empty or game_date not in rows.index:
                player_cells.append({
                    "played": False,
                    "stat_value": "",
                    "hit": False
                })
            else:
                row = rows.loc[game_date]

                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]

                odds_data = odds_lookup.get((player_name, game_date), {})

                match_rate = summaries[len(player_cells)]["hit_rate"]
                implied_prob = odds_data.get("implied_prob")

                edge_diff = None
                edge_label = "⚖️ Fair Price"

                if implied_prob is not None:
                    edge_diff = round(match_rate - implied_prob, 1)

                    if edge_diff >= 10:
                        edge_label = "🔥 Heavy Overpriced"
                    elif edge_diff >= 5:
                        edge_label = "✅ Overpriced"
                    elif edge_diff <= -10:
                        edge_label = "❌ Heavy Underpriced"
                    elif edge_diff <= -5:
                        edge_label = "⚠️ Underpriced"

                player_cells.append({
                    "played": True,
                    "stat_value": float(row["stat_value"]) if not pd.isna(row["stat_value"]) else 0,
                    "hit": bool(row["hit"]),
                    "odds": odds_data.get("odds"),
                    "odds_line": odds_data.get("line"),
                    "implied_prob": implied_prob,
                    "edge_diff": edge_diff,
                    "edge_label": edge_label
                })

        compare_rows.append({
            "game_date": game_date,
            "players": player_cells
        })

    return {
        "players": summaries,
        "rows": compare_rows,
        "prop_type": prop,
        "window": window,
        "filter_text": ftext
    }

def thresholds_for(role, prop):
    if role == "pitcher":
        return [3.5, 4.5, 5.5, 6.5, 7.5, 8.5]

    if prop == "home_runs":
        return [0.5, 1.5]

    if prop in ("hits", "runs", "rbi"):
        return [0.5, 1.5, 2.5, 3.5]

    return [0.5, 1.5, 2.5, 3.5, 4.5]


def pct_and_record(values, threshold):
    games = len(values)

    if games == 0:
        return 0, "0/0"

    wins = int((values > threshold).sum())
    pct = round((wins / games) * 100, 1)

    return pct, f"{wins}/{games}"


def build_hitter_leaderboard(df, prop, window, thresholds, sort_line, limit=50):
    if df.empty:
        return []

    df = df.copy()
    df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
    df = df.dropna(subset=["game_date", "batter_name"])

    grouped = (
        df.groupby(["batter_name", "game_date"], as_index=False)
        .agg({
            "team": "last",
            "h": "sum",
            "tb": "sum",
            "hr": "sum",
            "runs_scored": "sum",
            "rbi": "sum"
        })
        .sort_values(["batter_name", "game_date"], ascending=[True, False])
    )

    grouped["stat_value"] = calculate_hitter_stat(grouped, prop)

    rows = []

    for player_name, player_df in grouped.groupby("batter_name"):
        recent = player_df.sort_values("game_date", ascending=False).head(window)

        if len(recent) < 3:
            continue

        item = {
            "player_name": player_name,
            "team": clean_text(recent["team"].dropna().iloc[0]) if not recent["team"].dropna().empty else "",
            "games": len(recent),
            "average": round(float(recent["stat_value"].mean()), 2)
        }

        for t in thresholds:
            key = str(t).replace(".", "_")
            pct, rec = pct_and_record(recent["stat_value"], t)
            item[f"over_{key}"] = pct
            item[f"record_{key}"] = rec

        rows.append(item)

    sort_key = f"over_{str(sort_line).replace('.', '_')}"

    rows.sort(
        key=lambda x: (x.get(sort_key, 0), x.get("average", 0), x.get("games", 0)),
        reverse=True
    )

    return rows[:limit]


def build_pitcher_leaderboard(df, prop, window, thresholds, sort_line, limit=50):
    if df.empty or "player_name" not in df.columns:
        return []

    df = df.copy()
    df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
    df = df.dropna(subset=["game_date", "player_name"])

    df["stat_value"] = calculate_pitcher_stat(df, prop)

    rows = []

    for player_name, player_df in df.groupby("player_name"):
        recent = player_df.sort_values("game_date", ascending=False).head(window)

        if len(recent) < 3:
            continue

        item = {
            "player_name": player_name,
            "games": len(recent),
            "average": round(float(recent["stat_value"].mean()), 2)
        }

        for t in thresholds:
            key = str(t).replace(".", "_")
            pct, rec = pct_and_record(recent["stat_value"], t)
            item[f"over_{key}"] = pct
            item[f"record_{key}"] = rec

        rows.append(item)

    sort_key = f"over_{str(sort_line).replace('.', '_')}"

    rows.sort(
        key=lambda x: (x.get(sort_key, 0), x.get("average", 0), x.get("games", 0)),
        reverse=True
    )

    return rows[:limit]


def get_common_context(active_page="calculator"):
    error = ""

    calc_role = request.args.get("calc_role", "hitter")
    role = request.args.get("role", calc_role)

    calc_player = clean_text(request.args.get("calc_player", ""))
    calc_compare_2 = clean_text(request.args.get("calc_compare_2", ""))
    calc_compare_3 = clean_text(request.args.get("calc_compare_3", ""))

    calc_mode = request.args.get("calc_mode", "over")
    calc_prop = request.args.get("calc_prop", "hits")
    calc_window = safe_int(request.args.get("calc_window", 10), 10)
    calc_line = safe_float(request.args.get("calc_line", 0.5), 0.5)
    calc_min = safe_float(request.args.get("calc_min", 0), 0)
    calc_max = safe_float(request.args.get("calc_max", 0), 0)
    
    hitter_prop = request.args.get("hitter_prop", calc_prop if calc_role == "hitter" else "hits")
    pitcher_prop = request.args.get("pitcher_prop", calc_prop if calc_role == "pitcher" else "strikeouts")

    vs_team = clean_text(request.args.get("vs_team", ""))
    vs_hand = clean_text(request.args.get("vs_hand", "")).upper()

    if vs_hand not in ("", "R", "L"):
        vs_hand = ""

    leaderboard_limit = safe_int(request.args.get("leaderboard_limit", 50), 50)
    lineup_filter = request.args.get("lineup_filter", "all")
    compare_values = get_compare_values()
    custom_result = None
    compare_result = None
    leaderboard = []
    teams = []
    hitter_names = []
    pitcher_names = []
    lineup_map = {}
    weather_lookup = {}

    thresholds = thresholds_for(role, hitter_prop if role == "hitter" else pitcher_prop)
    sort_line = safe_float(request.args.get("sort_line", thresholds[0]), thresholds[0])
    ftext = filter_text(vs_team, vs_hand)
      
    try:
        hitter_names = get_hitter_names()
        pitcher_names = get_pitcher_names()
        lineup_map = get_today_lineups()

        try:
            weather_lookup = load_team_weather()
        except Exception as e:
            print("Weather lookup error:", e)
            weather_lookup = {}

        pa_df = pd.DataFrame()
        pitcher_df = pd.DataFrame()

        if calc_role == "hitter" or role == "hitter":
            pa_df_raw = get_pa_data()
            teams = get_teams_from_pa(pa_df_raw)
            pa_df = filter_hitter_df(pa_df_raw, vs_team, vs_hand)

            if active_page in ("leaderboard", "trends"):
                pa_df = apply_lineup_filter(pa_df, lineup_map, lineup_filter)

        if calc_role == "pitcher" or role == "pitcher":
            pitcher_df = get_pitcher_data()

        selected_players = []

        for i in range(1, 11):
            field_name = "calc_player" if i == 1 else f"calc_compare_{i}"
            player_name = clean_text(request.args.get(field_name, ""))

            if player_name and player_name not in selected_players:
                selected_players.append(player_name)

        if selected_players:
            if calc_role == "hitter":
                source_df = pa_df

                if len(selected_players) == 1:
                    rows = build_hitter_game_rows(
                        source_df,
                        selected_players[0],
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max
                    )

                    custom_result = summarize_player(
                        selected_players[0],
                        rows,
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max,
                        ftext
                    )
                    conn = get_conn()

                    try:
                        custom_result["pa_breakdown"] = get_pa_props_breakdown(
                            conn,
                            selected_players[0],
                            rolling_games=calc_window,
                            season=2026
                        )
                    finally:
                        conn.close()

                else:
                    compare_result = build_compare_result(
                        selected_players,
                        calc_role,
                        source_df,
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max,
                        ftext
                    )

            else:
                source_df = pitcher_df

                if len(selected_players) == 1:
                    rows = build_pitcher_game_rows(
                        source_df,
                        selected_players[0],
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max
                    )

                    custom_result = summarize_player(
                        selected_players[0],
                        rows,
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max,
                        ""
                    )

                else:
                    compare_result = build_compare_result(
                        selected_players,
                        calc_role,
                        source_df,
                        calc_prop,
                        calc_window,
                        calc_mode,
                        calc_line,
                        calc_min,
                        calc_max,
                        ""
                    )

        if role == "hitter":
            thresholds = thresholds_for(role, hitter_prop)

            if sort_line not in thresholds:
                sort_line = thresholds[0]

            leaderboard = build_hitter_leaderboard(
                pa_df,
                hitter_prop,
                calc_window,
                thresholds,
                sort_line,
                leaderboard_limit
            )

            for item in leaderboard:
                team = str(item.get("team", "")).strip()

                weather = weather_lookup.get(team, {})

                item["weather_display"] = weather.get("weather_display", "")
                item["opp_pitcher"] = weather.get("opp_pitcher", "")
                item["opp_pitcher_hand"] = weather.get("opp_pitcher_hand", "")

        else:
            thresholds = thresholds_for(role, pitcher_prop)

            if sort_line not in thresholds:
                sort_line = thresholds[0]

            leaderboard = build_pitcher_leaderboard(
                pitcher_df,
                pitcher_prop,
                calc_window,
                thresholds,
                sort_line,
                leaderboard_limit
            )

    except Exception as e:
        error = f"Error loading report: {e}"

    return {
        "active_page": active_page,
        "compare_values": compare_values,
        "error": error,
        "custom_result": custom_result,
        "compare_result": compare_result,
        "hitter_names": hitter_names,
        "pitcher_names": pitcher_names,
        "calc_role": calc_role,
        "role": role,
        "calc_prop": calc_prop,
        "calc_player": calc_player,
        "calc_compare_2": calc_compare_2,
        "calc_compare_3": calc_compare_3,
        "calc_window": calc_window,
        "calc_mode": calc_mode,
        "calc_line": calc_line,
        "calc_min": calc_min,
        "calc_max": calc_max,
        "hitter_prop": hitter_prop,
        "pitcher_prop": pitcher_prop,
        "teams": teams,
        "vs_team": vs_team,
        "vs_hand": vs_hand,
        "filter_text": ftext,
        "lineup_map": lineup_map,
        "lineup_filter": lineup_filter,
        "leaderboard": leaderboard,
        "thresholds": thresholds,
        "sort_line": sort_line,
        "leaderboard_limit": leaderboard_limit
    }


def build_trends_context():
    prop = request.args.get("trend_prop", "hits")
    window = safe_int(request.args.get("trend_window", 10), 10)
    min_games = safe_int(request.args.get("min_games", 5), 5)
    vs_team = clean_text(request.args.get("vs_team", ""))
    vs_hand = clean_text(request.args.get("vs_hand", "")).upper()
    lineup_filter = request.args.get("lineup_filter", "all")

    if vs_hand not in ("", "R", "L"):
        vs_hand = ""

    df = get_pa_data()
    teams = get_teams_from_pa(df)
    lineup_map = get_today_lineups()

    df = filter_hitter_df(df, vs_team, vs_hand)
    df = apply_lineup_filter(df, lineup_map, lineup_filter)

    df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
    df = df.dropna(subset=["game_date", "batter_name"])

    grouped = (
        df.groupby(["batter_name", "game_date"], as_index=False)
        .agg({
            "team": "last",
            "h": "sum",
            "tb": "sum",
            "hr": "sum",
            "runs_scored": "sum",
            "rbi": "sum"
        })
        .sort_values(["batter_name", "game_date"], ascending=[True, False])
    )

    grouped["stat_value"] = calculate_hitter_stat(grouped, prop)

    hot = []
    cold = []
    movers = []

    for player_name, player_df in grouped.groupby("batter_name"):
        recent = player_df.sort_values("game_date", ascending=False).head(window)

        if len(recent) < min_games:
            continue

        hits = int((recent["stat_value"] > 0.5).sum())
        games = len(recent)
        rate = round((hits / games) * 100, 1)
        avg = round(float(recent["stat_value"].mean()), 2)

        streak = 0

        for _, row in recent.iterrows():
            if row["stat_value"] > 0.5:
                streak += 1
            else:
                break

        team = clean_text(recent["team"].dropna().iloc[0]) if not recent["team"].dropna().empty else ""

        item = {
            "player_name": player_name,
            "team": team,
            "games": games,
            "hits": hits,
            "rate": rate,
            "avg": avg,
            "streak": streak,
            "last_date": recent.iloc[0]["game_date"].strftime("%Y-%m-%d"),
            "last_value": recent.iloc[0]["stat_value"]
        }

        if streak >= 3 or rate >= 70:
            hot.append(item)

        if rate <= 30:
            cold.append(item)

        recent_half = recent.head(max(3, window // 2))
        older_half = recent.tail(max(3, window // 2))

        recent_rate = (recent_half["stat_value"] > 0.5).mean() * 100
        older_rate = (older_half["stat_value"] > 0.5).mean() * 100
        jump = round(recent_rate - older_rate, 1)

        move_item = dict(item)
        move_item["jump"] = jump

        if jump > 0:
            movers.append(move_item)

    hot = sorted(hot, key=lambda x: (x["streak"], x["rate"], x["avg"]), reverse=True)[:25]
    cold = sorted(cold, key=lambda x: (x["rate"], x["avg"]))[:25]
    movers = sorted(movers, key=lambda x: x["jump"], reverse=True)[:25]

    return {
        "trend_prop": prop,
        "trend_window": window,
        "min_games": min_games,
        "trend_teams": teams,
        "trend_vs_team": vs_team,
        "trend_vs_hand": vs_hand,
        "lineup_filter": lineup_filter,
        "lineup_map": lineup_map,
        "hot_trends": hot,
        "cold_trends": cold,
        "mover_trends": movers
    }
def build_combo_context():
    combo_prop = request.args.get("combo_prop", "hits")
    combo_size = safe_int(request.args.get("combo_size", 2), 2)
    combo_window = safe_int(request.args.get("combo_window", 30), 30)
    combo_limit = safe_int(request.args.get("combo_limit", 50), 50)

    combo_limit = min(combo_limit, 100)
    lineup_map = get_today_lineups()

    try:
        df = read_sql("""
            SELECT
                players,
                teams,
                games,
                all_hit,
                rate,
                expected,
                edge
            FROM mlb_combo_results
            WHERE run_date = (
                SELECT MAX(run_date)
                FROM mlb_combo_results
            )
              AND prop = %s
              AND combo_size = %s
              AND window_games = %s
            ORDER BY rate DESC, games DESC, edge DESC
            LIMIT %s
        """, (
            combo_prop,
            combo_size,
            combo_window,
            combo_limit
        ))

        combo_rows = []

        for _, row in df.iterrows():
            player_names = str(row["players"]).split(" | ")
            team_names = str(row["teams"]).split(" | ")

            players = []
            for i, name in enumerate(player_names):
                players.append({
                    "name": name,
                    "team": team_names[i] if i < len(team_names) else ""
                })

            combo_rows.append({
                "players": players,
                "teams": row["teams"],
                "games": int(row["games"]),
                "all_hit": int(row["all_hit"]),
                "rate": float(row["rate"]),
                "expected": float(row["expected"]),
                "edge": float(row["edge"]),
            })

    except Exception as e:
        print("Combo read error:", e)
        combo_rows = []

    return {
        "combo_rows": combo_rows,
        "combo_prop": combo_prop,
        "combo_size": combo_size,
        "combo_window": combo_window,
        "combo_limit": combo_limit,
        "lineup_map": lineup_map
    }

def get_pa_props_breakdown(conn, player_name, prop="HITS", rolling_games=30, season=2026):
    sql = """
    WITH recent_games AS (
        SELECT DISTINCT game_date
        FROM mlb_pa_gamelog
        WHERE batter_name = %(player)s
          AND season = %(season)s::text
        ORDER BY game_date DESC
        LIMIT %(rolling_games)s
    ),
    pa_rows AS (
        SELECT
            game_date,
            batter_name,
            ROW_NUMBER() OVER (
                PARTITION BY game_date, batter_name
                ORDER BY pa_index
            ) AS pa_number,
            CASE
                WHEN bb = 1 OR hbp = 1 THEN 'Walk/HBP'
                WHEN single = 1 THEN 'Single'
                WHEN double = 1 OR triple = 1 OR hr = 1 THEN 'XBH'
                WHEN so = 1 THEN 'SO'
                WHEN ab = 1 AND h = 0 THEN 'Other Out'
                ELSE 'Other'
            END AS result_bucket
        FROM mlb_pa_gamelog
        WHERE batter_name = %(player)s
          AND season = %(season)s::text
          AND game_date IN (SELECT game_date FROM recent_games)
    )
    SELECT
        CASE
            WHEN pa_number = 1 THEN '1st PA'
            WHEN pa_number = 2 THEN '2nd PA'
            WHEN pa_number = 3 THEN '3rd PA'
            WHEN pa_number = 4 THEN '4th PA'
            ELSE '5+ PA'
        END AS pa_slot,
        result_bucket,
        COUNT(*) AS hits,
        COUNT(DISTINCT game_date) AS games_with_result,
        (SELECT COUNT(*) FROM recent_games) AS games_sample
    FROM pa_rows
    WHERE result_bucket IN ('Walk/HBP', 'Single', 'XBH', 'SO', 'Other Out')
    GROUP BY pa_slot, result_bucket
    ORDER BY
        CASE pa_slot
            WHEN '1st PA' THEN 1
            WHEN '2nd PA' THEN 2
            WHEN '3rd PA' THEN 3
            WHEN '4th PA' THEN 4
            ELSE 5
        END,
        result_bucket;
    """

    df = pd.read_sql(sql, conn, params={
        "player": player_name,
        "season": season,
        "rolling_games": rolling_games
    })

    rows = []
    for _, r in df.iterrows():
        games = int(r["games_sample"] or 0)
        count = int(r["hits"] or 0)

        rows.append({
            "pa_slot": r["pa_slot"],
            "result": r["result_bucket"],
            "count": count,
            "games": games,
            "avg": round(count / games, 3) if games else 0,
            "hit_rate": round((count / games) * 100, 1) if games else 0
        })

    return rows

@app.route("/api/odds/snapshot", methods=["POST"])
def odds_snapshot():
    try:
        api_key = request.headers.get("X-API-KEY")

        if api_key != ODDS_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        rows = data.get("rows", [])

        if not rows:
            return jsonify({
                "success": True,
                "inserted": 0,
                "last_seen_updated": 0,
                "closed": 0,
                "deleted_from_last_seen": 0
            }), 200

        run_id = data.get("run_id") or str(uuid.uuid4())
        is_final_chunk = bool(data.get("is_final_chunk"))

        snapshot_sql = """
            INSERT INTO odds_snapshots (
                player, sportsbook, lastupdate, islive, prop, ou,
                line, odds, ismain, starttime, gameid, home, away
            )
            VALUES (
                %(player)s, %(sportsbook)s, %(lastupdate)s, %(islive)s, %(prop)s, %(ou)s,
                %(line)s, %(odds)s, %(ismain)s, %(starttime)s, %(gameid)s, %(home)s, %(away)s
            )
        """

        last_seen_sql = """
            INSERT INTO odds_last_seen (
                player, sportsbook, lastupdate, islive, prop, ou,
                line, odds, ismain, starttime, gameid, home, away,
                updated_at, seen_run_id
            )
            VALUES (
                %(player)s, %(sportsbook)s, %(lastupdate)s, %(islive)s, %(prop)s, %(ou)s,
                %(line)s, %(odds)s, %(ismain)s, %(starttime)s, %(gameid)s, %(home)s, %(away)s,
                NOW(), %(seen_run_id)s
            )
            ON CONFLICT (gameid, player, sportsbook, prop, ou)
            DO UPDATE SET
                updated_at = NOW(),
                lastupdate = EXCLUDED.lastupdate,
                islive = EXCLUDED.islive,
                line = EXCLUDED.line,
                odds = EXCLUDED.odds,
                ismain = EXCLUDED.ismain,
                starttime = EXCLUDED.starttime,
                home = EXCLUDED.home,
                away = EXCLUDED.away,
                seen_run_id = EXCLUDED.seen_run_id
        """

        close_sql = """
            INSERT INTO closing_odds (
                player, sportsbook, lastupdate, islive,
                prop, ou, line, odds, ismain,
                starttime, gameid, home, away, closed_at
            )
            SELECT
                player, sportsbook, lastupdate, islive,
                prop, ou, line, odds, ismain,
                starttime, gameid, home, away, NOW()
            FROM odds_last_seen
            WHERE seen_run_id IS NOT NULL
              AND seen_run_id != %s
            ON CONFLICT (gameid, player, sportsbook, prop, ou)
            DO NOTHING
        """

        delete_sql = """
            DELETE FROM odds_last_seen
            WHERE seen_run_id IS NOT NULL
              AND seen_run_id != %s
        """

        clean_rows = []

        for r in rows:
            clean_rows.append({
                "player": clean_text(r.get("player"), None),
                "sportsbook": clean_text(r.get("sportsbook"), None),
                "lastupdate": safe_int(r.get("lastupdate")),
                "islive": safe_int(r.get("islive")),
                "prop": clean_text(r.get("prop"), None),
                "ou": clean_text(r.get("ou"), None),
                "line": safe_float(r.get("line")),
                "odds": safe_int(r.get("odds")),
                "ismain": safe_int(r.get("ismain")),
                "starttime": clean_text(r.get("starttime"), None),
                "gameid": clean_text(r.get("gameid"), None),
                "home": clean_text(r.get("home"), None),
                "away": clean_text(r.get("away"), None),
                "seen_run_id": run_id
            })

        conn = psycopg2.connect(DATABASE_URL)

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.executemany(snapshot_sql, clean_rows)
                    cur.executemany(last_seen_sql, clean_rows)

                    closed_count = 0
                    deleted_count = 0

                    if is_final_chunk:
                        cur.execute(close_sql, (run_id,))
                        closed_count = cur.rowcount

                        cur.execute(delete_sql, (run_id,))
                        deleted_count = cur.rowcount

            return jsonify({
                "success": True,
                "run_id": run_id,
                "inserted": len(clean_rows),
                "last_seen_updated": len(clean_rows),
                "closed": closed_count,
                "deleted_from_last_seen": deleted_count
            }), 200

        finally:
            conn.close()

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

def clean_text(value, default=""):
    if value in ("", None):
        return default
    return str(value).strip()


def safe_int(value, default=None):
    if value in ("", None):
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=None):
    if value in ("", None):
        return default
    try:
        return float(value)
    except Exception:
        return default

def load_team_weather():

    conn = get_conn()

    query = """
    SELECT *
    FROM mlb_game_context
    WHERE game_date = (
        SELECT MAX(game_date)
        FROM mlb_game_context
    )
    """

    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()

    weather_lookup = {}

    for _, row in df.iterrows():
        team_code = str(row["team_code"]).strip()
        weather_lookup[team_code] = row.to_dict()

    print("Weather teams loaded:", list(weather_lookup.keys()))

    return weather_lookup

@app.route("/")
def index():
    context = get_common_context(active_page="calculator")
    return render_template("index.html", **context)

@app.route("/combos")
def combos_page():
    context = get_common_context(active_page="combos")
    context.update(build_combo_context())
    return render_template("combos.html", **context)

@app.route("/leaderboard")
def leaderboard_page():
    context = get_common_context(active_page="leaderboard")
    return render_template("leaderboard.html", **context)


@app.route("/trends")
def trends_page():
    context = get_common_context(active_page="trends")
    context.update(build_trends_context())
    return render_template("trends.html", **context)


@app.route("/share")
def share_page():
    context = get_common_context(active_page="share")
    return render_template("share.html", **context)


@app.route("/clear_cache")
def clear_cache():
    cache.clear()
    return "Cache cleared"


if __name__ == "__main__":
    app.run(debug=True)