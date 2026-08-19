from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import json
from flask_caching import Cache
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required
)

from database import (
    get_conn,
    read_sql,
    get_systems,
    is_watching_system,
    watch_system,
    unwatch_system,
    create_system_record,
    system_code_exists,
    create_combo_system
)

import unicodedata
import pandas as pd
import requests
import re
import os
import uuid

from datetime import date, timedelta, datetime
import secrets
from urllib.parse import urlencode
from functools import wraps
from services.grading_engine import grade_bet, regrade_bet
from services.auto_grading import grade_pending_mlb_bets
from services.market_registry import resolve_market, MARKETS
from zoneinfo import ZoneInfo

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_INVITE_URL = os.environ.get("DISCORD_INVITE_URL")

DISCORD_API_URL = "https://discord.com/api/v10"
app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(
        self,
        id,
        discord_id,
        username,
        avatar,
        membership_tier="free",
        community_status="member",
        is_beta_tester=False,
        is_admin=False
    ):
        self.id = id
        self.discord_id = discord_id
        self.username = username
        self.avatar = avatar

        self.membership_tier = membership_tier
        self.community_status = community_status
        self.is_beta_tester = bool(is_beta_tester)
        self.is_admin = bool(is_admin)

    @property
    def is_premium(self):
        return self.membership_tier in {
            "premium",
            "premium_plus"
        }

    @property
    def is_premium_plus(self):
        return self.membership_tier == "premium_plus"

    @property
    def has_capper_access(self):
        return self.community_status in {
            "capper_plus",
            "elite_capper"
        }

    @property
    def has_advanced_access(self):
        return (
            self.is_admin
            or self.is_beta_tester
            or self.is_premium_plus
            or self.has_capper_access
        )

def premium_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for(
                "login",
                next=request.url
            ))

        if not current_user.is_premium and not current_user.is_admin:
            return redirect(url_for(
                "upgrade_page",
                required="premium"
            ))

        return view_function(*args, **kwargs)

    return wrapped_view


def premium_plus_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for(
                "login",
                next=request.url
            ))

        if not current_user.has_advanced_access:
            return redirect(url_for(
                "upgrade_page",
                required="premium_plus"
            ))

        return view_function(*args, **kwargs)

    return wrapped_view


def capper_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for(
                "login",
                next=request.url
            ))

        if not (
            current_user.has_capper_access
            or current_user.is_admin
        ):
            return redirect(url_for(
                "upgrade_page",
                required="capper"
            ))

        return view_function(*args, **kwargs)

    return wrapped_view


def beta_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for(
                "login",
                next=request.url
            ))

        if not (
            current_user.is_beta_tester
            or current_user.is_admin
        ):
            return redirect(url_for(
                "upgrade_page",
                required="beta"
            ))

        return view_function(*args, **kwargs)

    return wrapped_view


def admin_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for(
                "login",
                next=request.url
            ))

        if not current_user.is_admin:
            return render_template(
                "403.html",
                active_page=""
            ), 403

        return view_function(*args, **kwargs)

    return wrapped_view

@login_manager.user_loader
def load_user(user_id):
    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT
                id,
                discord_id,
                username,
                avatar,
                membership_tier,
                community_status,
                is_beta_tester,
                is_admin
            FROM users
            WHERE id = %s
        """, (user_id,))

        row = cur.fetchone()

        if not row:
            return None

        return User(
            id=row[0],
            discord_id=row[1],
            username=row[2],
            avatar=row[3],
            membership_tier=row[4],
            community_status=row[5],
            is_beta_tester=row[6],
            is_admin=row[7]
        )     

    finally:
        conn.close()

cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300
})

MAX_COMPARE_PLAYERS = 10

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


def generate_ticket_title(legs):
    """Generate a readable title when the user leaves the title blank."""
    if not legs:
        return "Manual Bet"

    def name_for(leg):
        return clean_text(
            leg.get("selection_name")
            or leg.get("player_name")
            or leg.get("team_name")
            or "Selection"
        )

    def format_line(value):
        if value in (None, ""):
            return ""
        try:
            number = float(value)
            return str(int(number)) if number.is_integer() else f"{number:g}"
        except (TypeError, ValueError):
            return clean_text(value)

    if len(legs) == 1:
        leg = legs[0]
        name = name_for(leg)
        side = clean_text(leg.get("ou")).title()
        line = format_line(
            leg.get("user_line")
            if leg.get("user_line") is not None
            else leg.get("line")
        )
        prop = clean_text(leg.get("prop") or leg.get("selection_type"))

        return " ".join(
            part for part in (name, side, line, prop) if part
        ) or "Manual Bet"

    short_names = []
    for leg in legs:
        full_name = name_for(leg)
        parts = full_name.split()
        short_names.append(parts[-1] if parts else "Selection")

    if len(short_names) <= 3:
        return " + ".join(short_names)

    return f"{' + '.join(short_names[:3])} + {len(short_names) - 3} More"


def calculate_bet_streaks(results):
    """Calculate current and longest ticket win/loss streaks."""
    normalized = [
        str(result or "").strip().lower()
        for result in results
        if str(result or "").strip().lower() in {"won", "lost"}
    ]

    if not normalized:
        return {
            "current_type": "",
            "current_count": 0,
            "longest_win": 0,
            "longest_loss": 0
        }

    current_type = normalized[0]
    current_count = 0

    for result in normalized:
        if result == current_type:
            current_count += 1
        else:
            break

    longest_win = 0
    longest_loss = 0
    running_type = None
    running_count = 0

    for result in reversed(normalized):
        if result == running_type:
            running_count += 1
        else:
            running_type = result
            running_count = 1

        if result == "won":
            longest_win = max(longest_win, running_count)
        else:
            longest_loss = max(longest_loss, running_count)

    return {
        "current_type": current_type,
        "current_count": current_count,
        "longest_win": longest_win,
        "longest_loss": longest_loss
    }


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
            "longest_hit_streak": 0,
            "streaks_4_plus": 0,
            "streak_rating": 0,
            "streak_profile": "No Data",
            "streak_distribution": [],
        }

    results = [bool(r) for r in results]

    first = results[0]
    current_count = 0

    for r in results:
        if r == first:
            current_count += 1
        else:
            break

    hit_streaks = []
    miss_streaks = []

    cur_hit = 0
    cur_miss = 0

    for r in results:
        if r:
            cur_hit += 1

            if cur_miss > 0:
                miss_streaks.append(cur_miss)
                cur_miss = 0
        else:
            cur_miss += 1

            if cur_hit > 0:
                hit_streaks.append(cur_hit)
                cur_hit = 0

    if cur_hit > 0:
        hit_streaks.append(cur_hit)

    if cur_miss > 0:
        miss_streaks.append(cur_miss)

    best_hit = max(hit_streaks) if hit_streaks else 0
    best_miss = max(miss_streaks) if miss_streaks else 0

    streaks_4_plus = sum(1 for s in hit_streaks if s >= 4)

    distribution_counts = {
        1: sum(1 for s in hit_streaks if s == 1),
        2: sum(1 for s in hit_streaks if s == 2),
        3: sum(1 for s in hit_streaks if s == 3),
        4: sum(1 for s in hit_streaks if s == 4),
        5: sum(1 for s in hit_streaks if s == 5),
        6: sum(1 for s in hit_streaks if s >= 6),
    }

    max_count = max(distribution_counts.values()) if distribution_counts else 0

    streak_distribution = []

    for length, count in distribution_counts.items():
        label = "6+" if length == 6 else str(length)

        streak_distribution.append({
            "length": label,
            "count": count,
            "percent": round((count / max_count) * 100, 1) if max_count else 0
        })

    games = len(results)
    hit_rate = sum(results) / games if games else 0
    avg_hit_streak = sum(hit_streaks) / len(hit_streaks) if hit_streaks else 0

    rating = 0
    rating += hit_rate * 40
    rating += min(best_hit / 10, 1) * 25
    rating += min(avg_hit_streak / 4, 1) * 20
    rating += min(streaks_4_plus / 5, 1) * 15

    streak_rating = round(rating)

    if streak_rating >= 85 and streaks_4_plus >= 3:
        streak_profile = "Proven Sustainer"
    elif current_count >= 4 and streaks_4_plus <= 1:
        streak_profile = "Hot Right Now"
    elif avg_hit_streak <= 1.7 and best_hit <= 4:
        streak_profile = "Flash Hitter"
    elif best_hit >= 7 and avg_hit_streak >= 2.5:
        streak_profile = "Momentum Builder"
    elif hit_rate <= 0.35:
        streak_profile = "Streak Breaker"
    else:
        streak_profile = "Average Trend"

    return {
        "current_streak_type": "hit" if first else "miss",
        "current_streak_count": current_count,
        "best_hit_streak": best_hit,
        "best_miss_streak": best_miss,

        "longest_hit_streak": best_hit,
        "streaks_4_plus": streaks_4_plus,
        "streak_rating": streak_rating,
        "streak_profile": streak_profile,
        "streak_distribution": streak_distribution,
    }

@cache.cached(timeout=300, key_prefix="pa_data_v2")
def get_pa_data():
    return read_sql("""
        SELECT
            batter_name,
            team,
            opp_team,
            pitcher_hand,
            day_night,
            game_date,
            COALESCE(h, 0) AS h,
            COALESCE(single, 0) AS single,
            COALESCE(double, 0) AS double,
            COALESCE(tb, 0) AS tb,
            COALESCE(hr, 0) AS hr,
            COALESCE(bb, 0) AS bb,
            COALESCE(sb, 0) AS sb,
            COALESCE(runs_scored, 0) AS runs_scored,
            COALESCE(rbi, 0) AS rbi
        FROM mlb_pa_gamelog
        WHERE batter_name IS NOT NULL
          AND game_date IS NOT NULL
          AND season IN ('2025', '2026')
    """)

def get_pa_data_for_players(players):
    if not players:
        return pd.DataFrame()

    cleaned_players = [
        clean_text(player)
        for player in players
        if clean_text(player)
    ]

    if not cleaned_players:
        return pd.DataFrame()

    placeholders = ",".join(
        ["%s"] * len(cleaned_players)
    )

    query = f"""
        SELECT
            batter_name,
            team,
            opp_team,
            pitcher_hand,
            day_night,
            game_date,
            COALESCE(h, 0) AS h,
            COALESCE(single, 0) AS single,
            COALESCE(double, 0) AS double,
            COALESCE(tb, 0) AS tb,
            COALESCE(hr, 0) AS hr,
            COALESCE(bb, 0) AS bb,
            COALESCE(sb, 0) AS sb,
            COALESCE(runs_scored, 0) AS runs_scored,
            COALESCE(rbi, 0) AS rbi
        FROM mlb_pa_gamelog
        WHERE batter_name IS NOT NULL
          AND game_date IS NOT NULL
          AND season IN ('2025', '2026')
          AND batter_name IN ({placeholders})
    """

    return read_sql(
        query,
        cleaned_players,
    )

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
    market = resolve_market(display_prop=prop)

    source = market.source if market and market.entity == "batter" else ""

    if source == "h":
        return pd.to_numeric(df["h"], errors="coerce").fillna(0)
    if source == "single":
        return pd.to_numeric(df["single"], errors="coerce").fillna(0)
    if source == "double":
        return pd.to_numeric(df["double"], errors="coerce").fillna(0)
    if source == "tb":
        return pd.to_numeric(df["tb"], errors="coerce").fillna(0)
    if source == "hr":
        return pd.to_numeric(df["hr"], errors="coerce").fillna(0)
    if source == "bb":
        return pd.to_numeric(df["bb"], errors="coerce").fillna(0)
    if source == "sb":
        return pd.to_numeric(df["sb"], errors="coerce").fillna(0)
    if source == "runs_scored":
        return pd.to_numeric(df["runs_scored"], errors="coerce").fillna(0)
    if source == "rbi":
        return pd.to_numeric(df["rbi"], errors="coerce").fillna(0)
    if source == "h+runs_scored+rbi":
        return (
            pd.to_numeric(df["h"], errors="coerce").fillna(0)
            + pd.to_numeric(df["runs_scored"], errors="coerce").fillna(0)
            + pd.to_numeric(df["rbi"], errors="coerce").fillna(0)
        )

    return pd.to_numeric(df["h"], errors="coerce").fillna(0)


def innings_to_outs(value):
    """Convert baseball IP notation (e.g. 5.2) into recorded outs."""
    try:
        innings = float(value)
    except (TypeError, ValueError):
        return 0

    whole = int(innings)
    partial = round(innings - whole, 1)

    if abs(partial - 0.1) < 0.01:
        extra_outs = 1
    elif abs(partial - 0.2) < 0.01:
        extra_outs = 2
    else:
        extra_outs = 0

    return (whole * 3) + extra_outs


def calculate_pitcher_stat(df, prop):
    market = resolve_market(display_prop=prop)

    source = market.source if market and market.entity == "pitcher" else ""

    if source == "strikeouts":
        return pd.to_numeric(df["strikeouts"], errors="coerce").fillna(0)
    if source == "earned_runs":
        return pd.to_numeric(df["earned_runs"], errors="coerce").fillna(0)
    if source == "hits_allowed":
        return pd.to_numeric(df["hits_allowed"], errors="coerce").fillna(0)
    if source == "walks_allowed":
        return pd.to_numeric(df["walks_allowed"], errors="coerce").fillna(0)
    if source == "runs_allowed":
        return pd.to_numeric(df["runs_allowed"], errors="coerce").fillna(0)
    if source == "outs_from_innings":
        return df["innings_pitched"].apply(innings_to_outs)

    return pd.Series([0] * len(df), index=df.index)


def filter_hitter_df(df, vs_team="", vs_hand="", day_night=""):
    out = df

    if vs_team:
        out = out[out["opp_team"].astype(str) == vs_team]

    if vs_hand:
        out = out[out["pitcher_hand"].astype(str).str.upper() == vs_hand.upper()]

    if day_night:
        out = out[out["day_night"].astype(str).str.lower() == day_night]

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


def filter_text(vs_team="", vs_hand="", weekday="all", day_night=""):
    parts = []

    if vs_team:
        parts.append(f"Vs {vs_team}")

    if vs_hand == "R":
        parts.append("Vs RHP")
    elif vs_hand == "L":
        parts.append("Vs LHP")

    if day_night == "day":
        parts.append("Day Games")

    elif day_night == "night":
        parts.append("Night Games")

    weekday_names = {
        "0": "Monday",
        "1": "Tuesday",
        "2": "Wednesday",
        "3": "Thursday",
        "4": "Friday",
        "5": "Saturday",
        "6": "Sunday"
    }

    if weekday != "all":
        parts.append(weekday_names.get(str(weekday), ""))

    return " • ".join([p for p in parts if p])


def hit_check(value, mode, line, min_value, max_value):
    if mode == "under":
        return value < line
    if mode == "range":
        return min_value <= value <= max_value
    return value > line


def build_hitter_game_rows(df, player_name, prop, window, mode, line, min_value, max_value, weekday="all"):
    player_df = df[df["batter_name"].astype(str) == player_name].copy()

    if player_df.empty:
        return pd.DataFrame(columns=["game_date", "stat_value", "hit"])

    player_df["game_date"] = pd.to_datetime(player_df["game_date"], errors="coerce")
    player_df = player_df.dropna(subset=["game_date"])

    if weekday != "all":
        player_df = player_df[player_df["game_date"].dt.dayofweek == int(weekday)]

    grouped = (
        player_df
        .groupby("game_date", as_index=False)
        .agg({
            "h": "sum",
            "single": "sum",
            "double": "sum",
            "tb": "sum",
            "hr": "sum",
            "bb": "sum",
            "sb": "sum",
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
        "longest_hit_streak": streaks["longest_hit_streak"],
        "streaks_4_plus": streaks["streaks_4_plus"],
        "streak_rating": streaks["streak_rating"],
        "streak_profile": streaks["streak_profile"],
        "streak_distribution": streaks["streak_distribution"],

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

def get_live_system_legs(combo_legs):
    """
    Load today's available odds for every player in a saved combo.

    Returns a pandas DataFrame containing all available lines so the
    qualifier engine can later choose the correct prop, O/U and line.
    """

    if not combo_legs:
        return pd.DataFrame()

    players = []

    for leg in combo_legs:
        player_name = clean_text(leg.get("player_name"))

        if player_name and player_name not in players:
            players.append(player_name)

    if not players:
        return pd.DataFrame()

    placeholders = ",".join(["%s"] * len(players))

    sql = f"""
        SELECT
            player,
            sportsbook,
            prop,
            ou,
            line,
            odds,
            ismain,
            islive,
            starttime,
            gameid,
            home,
            away,
            lastupdate
        FROM odds_last_seen
        WHERE player IN ({placeholders})
          AND odds IS NOT NULL
          AND line IS NOT NULL
          AND COALESCE(islive, 0) = 0
          AND DATE(starttime) = CURRENT_DATE
        ORDER BY
            player,
            prop,
            ou,
            sportsbook,
            lastupdate DESC
    """

    try:
        return read_sql(sql, players)

    except Exception as e:
        print("Live system odds error:", e)
        return pd.DataFrame()

def american_to_decimal(odds):
    """
    Convert American odds into decimal odds.

    Examples:
        -200 -> 1.50
        +150 -> 2.50
    """
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return None

    if odds == 0:
        return None

    if odds > 0:
        return 1 + (odds / 100)

    return 1 + (100 / abs(odds))


def decimal_to_american(decimal_odds):
    """
    Convert decimal odds back into American odds.
    """
    try:
        decimal_odds = float(decimal_odds)
    except (TypeError, ValueError):
        return None

    if decimal_odds <= 1:
        return None

    if decimal_odds >= 2:
        return round((decimal_odds - 1) * 100)

    return round(-100 / (decimal_odds - 1))


def normalize_system_prop(prop):
    """
    Make saved prop names and live odds prop names match.
    """
    value = clean_text(prop).upper().replace(" ", "_")

    aliases = {
        "HIT": "HITS",
        "HITS": "HITS",

        "TOTAL_BASE": "TB",
        "TOTAL_BASES": "TB",
        "TOTALBASES": "TB",
        "TB": "TB",

        "HOME_RUN": "HR",
        "HOME_RUNS": "HR",
        "HOMERUNS": "HR",
        "HR": "HR",

        "RUN": "RUNS",
        "RUNS": "RUNS",

        "RBI": "RBI",
        "RBIS": "RBI",

        "STRIKEOUT": "SO",
        "STRIKEOUTS": "SO",
        "PITCHER_STRIKEOUTS": "SO",
        "SO": "SO",
    }

    return aliases.get(value, value)


def check_saved_system(
    combo,
    combo_legs,
    preferred_sportsbook="fanduel"
):
    """
    Check a saved combo against today's current odds.

    Returns:
        qualified
        failed
        waiting
    """

    result = {
        "status": "waiting",
        "qualified": False,
        "message": "Waiting for today's markets.",
        "sportsbook": preferred_sportsbook,
        "combined_odds": None,
        "minimum_required": None,
        "odds_passed": None,
        "all_legs_passed": False,
        "legs": []
    }

    if not combo:
        result["status"] = "failed"
        result["message"] = "No saved combo is attached to this system."
        return result

    if not combo_legs:
        result["status"] = "failed"
        result["message"] = "This combo does not contain any saved legs."
        return result

    minimum_required = combo.get("minimum_combined_odds")
    require_exact_lines = bool(combo.get("require_exact_lines"))
    require_all_active = bool(combo.get("require_all_active"))

    result["minimum_required"] = minimum_required

    live_df = get_live_system_legs(combo_legs)
    lineup_map = get_today_lineups()

    if live_df.empty:
        for leg in combo_legs:
            result["legs"].append({
                "player_name": leg.get("player_name"),
    		"prop": normalize_system_prop(leg.get("prop")),
    		"ou": clean_text(leg.get("ou")).lower(),

    		"saved_line": leg.get("line"),
    		"current_line": None,
    		"current_odds": None,

    		"sportsbook": preferred_sportsbook,
	
    		"game_id": None,
    		"home_team": None,
    		"away_team": None,
    		"start_time": None,
    		"last_update": None,

    		"active_status": "waiting",
    		"market_status": "waiting",
    		"status": "waiting",
    		"passed": False,
    		"reason": "No current odds are available."
	})

        return result

    live_df = live_df.copy()

    live_df["_player_key"] = (
        live_df["player"]
        .fillna("")
        .astype(str)
        .apply(normalize_name)
    )

    live_df["_prop_key"] = (
        live_df["prop"]
        .fillna("")
        .astype(str)
        .apply(normalize_system_prop)
    )

    live_df["_ou_key"] = (
        live_df["ou"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    live_df["_sportsbook_key"] = (
        live_df["sportsbook"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    live_df["line"] = pd.to_numeric(
        live_df["line"],
        errors="coerce"
    )

    live_df["odds"] = pd.to_numeric(
        live_df["odds"],
        errors="coerce"
    )

    selected_decimal_odds = []
    has_failed_leg = False
    has_waiting_leg = False

    active_values = {
        "confirmed",
        "probable",
        "active",
        "starting"
    }

    inactive_values = {
        "inactive",
        "out",
        "not_starting",
        "not starting",
        "scratched"
    }

    for saved_leg in combo_legs:
        player_name = clean_text(saved_leg.get("player_name"))
        saved_prop = normalize_system_prop(saved_leg.get("prop"))
        saved_ou = clean_text(saved_leg.get("ou")).lower()

        try:
            saved_line = float(saved_leg.get("line"))
        except (TypeError, ValueError):
            saved_line = None

        leg_result = {
            "player_name": player_name,
            "prop": saved_prop,
            "ou": saved_ou,

            "saved_line": saved_line,
            "current_line": None,
            "current_odds": None,

            "sportsbook": preferred_sportsbook,

            "game_id": None,
            "home_team": None,
            "away_team": None,
            "start_time": None,
            "last_update": None,

            "active_status": "waiting",
            "market_status": "waiting",
            "status": "waiting",
            "passed": False,
            "reason": ""
        }

        # ---------------------------------------------------------
        # Check today's lineup status
        # ---------------------------------------------------------
        lineup_info = (
            lineup_map.get(player_name)
            or lineup_map.get(player_name.lower())
            or lineup_map.get(normalize_name(player_name))
        )

        if lineup_info:
            lineup_status = clean_text(
                lineup_info.get("status")
            ).lower()

            if lineup_status in active_values:
                leg_result["active_status"] = "passed"

            elif lineup_status in inactive_values:
                leg_result["active_status"] = "failed"

            else:
                leg_result["active_status"] = "waiting"
        else:
            leg_result["active_status"] = "waiting"

        # ---------------------------------------------------------
        # Locate matching market
        # ---------------------------------------------------------
        matches = live_df[
            (live_df["_player_key"] == normalize_name(player_name))
            & (live_df["_prop_key"] == saved_prop)
            & (live_df["_ou_key"] == saved_ou)
            & (
                live_df["_sportsbook_key"]
                == preferred_sportsbook.lower()
            )
        ].copy()

        if matches.empty:
            leg_result["market_status"] = "waiting"
            leg_result["status"] = "waiting"
            leg_result["reason"] = (
                f"No {preferred_sportsbook.title()} "
                f"{saved_prop} {saved_ou.title()} market is posted."
            )

            has_waiting_leg = True
            result["legs"].append(leg_result)
            continue

        # ---------------------------------------------------------
        # Match the saved line
        # ---------------------------------------------------------
        if saved_line is not None:
            exact_matches = matches[
                (matches["line"] - saved_line).abs() < 0.001
            ].copy()
        else:
            exact_matches = pd.DataFrame()

        if require_exact_lines:
            if exact_matches.empty:
                available_lines = sorted(
                    matches["line"]
                    .dropna()
                    .astype(float)
                    .unique()
                    .tolist()
                )

                leg_result["current_line"] = (
                    available_lines[0]
                    if available_lines
                    else None
                )

                leg_result["market_status"] = "failed"
                leg_result["status"] = "failed"
                leg_result["reason"] = (
                    f"Exact line {saved_line:g} is unavailable."
                    if saved_line is not None
                    else "The saved line is invalid."
                )

                if available_lines:
                    leg_result["reason"] += (
                        " Available line"
                        + ("s are " if len(available_lines) > 1 else " is ")
                        + ", ".join(
                            f"{line:g}"
                            for line in available_lines
                        )
                        + "."
                    )

                has_failed_leg = True
                result["legs"].append(leg_result)
                continue

            candidate_rows = exact_matches

        else:
            if not exact_matches.empty:
                candidate_rows = exact_matches

            elif saved_line is not None:
                matches["_line_distance"] = (
                    matches["line"] - saved_line
                ).abs()

                closest_distance = matches["_line_distance"].min()

                candidate_rows = matches[
                    matches["_line_distance"] == closest_distance
                ]

            else:
                candidate_rows = matches

        # Use the latest available row for the selected market.
        if "lastupdate" in candidate_rows.columns:
            candidate_rows = candidate_rows.sort_values(
                "lastupdate",
                ascending=False,
                na_position="last"
            )

        selected_row = candidate_rows.iloc[0]

        current_line = selected_row.get("line")
        current_odds = selected_row.get("odds")

        if pd.isna(current_line):
            current_line = None
        else:
            current_line = float(current_line)

        if pd.isna(current_odds):
            current_odds = None
        else:
            current_odds = int(current_odds)

        leg_result["current_line"] = current_line
        leg_result["current_odds"] = current_odds

        leg_result["sportsbook"] = selected_row.get(
            "sportsbook",
            preferred_sportsbook
        )

        leg_result["game_id"] = selected_row.get("gameid")
        leg_result["home_team"] = selected_row.get("home")
        leg_result["away_team"] = selected_row.get("away")
        leg_result["start_time"] = selected_row.get("starttime")
        leg_result["last_update"] = selected_row.get("lastupdate")

        if current_odds is None:
            leg_result["market_status"] = "waiting"
            leg_result["status"] = "waiting"
            leg_result["reason"] = "The line exists, but odds are unavailable."

            has_waiting_leg = True
            result["legs"].append(leg_result)
            continue

        leg_result["market_status"] = "passed"

        # ---------------------------------------------------------
        # Decide whether the complete leg passes
        # ---------------------------------------------------------
        if (
            require_all_active
            and leg_result["active_status"] == "failed"
        ):
            leg_result["status"] = "failed"
            leg_result["reason"] = (
                "Player is not active in today's lineup."
            )

            has_failed_leg = True

        elif (
            require_all_active
            and leg_result["active_status"] == "waiting"
        ):
            leg_result["status"] = "waiting"
            leg_result["reason"] = (
                "Waiting for lineup confirmation."
            )

            has_waiting_leg = True

        else:
            leg_result["status"] = "passed"
            leg_result["passed"] = True

            if (
                not require_exact_lines
                and saved_line is not None
                and current_line is not None
                and abs(current_line - saved_line) >= 0.001
            ):
                leg_result["reason"] = (
                    f"Alternate line {current_line:g} matched."
                )
            else:
                leg_result["reason"] = "Exact market matched."

            decimal_odds = american_to_decimal(current_odds)

            if decimal_odds is not None:
                selected_decimal_odds.append(decimal_odds)

        result["legs"].append(leg_result)

    # -------------------------------------------------------------
    # Calculate combined parlay odds
    # -------------------------------------------------------------
    if (
        selected_decimal_odds
        and len(selected_decimal_odds) == len(combo_legs)
    ):
        combined_decimal = 1.0

        for decimal_odds in selected_decimal_odds:
            combined_decimal *= decimal_odds

        result["combined_odds"] = decimal_to_american(
            combined_decimal
        )

    # -------------------------------------------------------------
    # Check minimum combined odds
    # -------------------------------------------------------------
    if minimum_required is None:
        result["odds_passed"] = True

    elif result["combined_odds"] is None:
        result["odds_passed"] = None
        has_waiting_leg = True

    else:
        result["odds_passed"] = (
            int(result["combined_odds"])
            >= int(minimum_required)
        )

        if not result["odds_passed"]:
            has_failed_leg = True

    result["all_legs_passed"] = all(
        leg["status"] == "passed"
        for leg in result["legs"]
    )

    # -------------------------------------------------------------
    # Final system status
    # -------------------------------------------------------------
    if has_failed_leg:
        result["status"] = "failed"
        result["qualified"] = False

        if result["odds_passed"] is False:
            result["message"] = (
                "The current combined odds do not meet "
                "the system minimum."
            )
        else:
            result["message"] = (
                "One or more saved legs did not qualify."
            )

    elif has_waiting_leg:
        result["status"] = "waiting"
        result["qualified"] = False
        result["message"] = (
            "Waiting for odds or lineup confirmation."
        )

    elif (
        result["all_legs_passed"]
        and result["odds_passed"] is True
    ):
        result["status"] = "qualified"
        result["qualified"] = True
        result["message"] = "This system qualifies today."

    else:
        result["status"] = "waiting"
        result["qualified"] = False
        result["message"] = "The system check is incomplete."

    return result

def american_to_implied_prob(odds):
    try:
        odds = int(odds)
    except Exception:
        return None

    if odds == 0:
        return None

    if odds > 0:
        return round((100 / (odds + 100)) * 100, 1)

    return round((abs(odds) / (abs(odds) + 100)) * 100, 1)


def implied_prob_to_american(probability_pct):
    """
    Convert an implied probability percentage back to American odds.

    Example:
        60.0 -> -150
        40.0 -> +150
    """
    try:
        p = float(probability_pct) / 100.0
    except (TypeError, ValueError):
        return None

    if p <= 0 or p >= 1:
        return None

    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))

    return int(round(100.0 * (1.0 - p) / p))


def get_historical_odds_lookup(
    players,
    prop,
    line,
    role="hitter",
    game_dates=None,
):
    """
    Load precomputed historical consensus odds for Compare Players.

    Reads provider_consensus_history instead of calculating sportsbook
    consensus from provider_market_history during a web request.

    Preference per player/date:
      1. close
      2. open
    """
    if not players:
        return {}

    market = resolve_market(display_prop=prop)

    if not market:
        return {}

    expected_entity = "batter" if role == "hitter" else "pitcher"

    if market.entity != expected_entity:
        return {}

    requested_players = [
        clean_text(player)
        for player in players
        if clean_text(player)
    ]

    if not requested_players:
        return {}

    visible_dates = sorted({
        clean_text(value)
        for value in (game_dates or [])
        if clean_text(value)
    })

    if not visible_dates:
        return {}

    player_placeholders = ",".join(
        ["%s"] * len(requested_players)
    )

    # Query one contiguous date range instead of generating a large
    # IN (...) list for 90/120-game Compare windows. The result is still
    # keyed by player/date below, so dates without a displayed game are
    # harmless and never appear in the Compare table.
    min_date = min(visible_dates)
    max_date = max(visible_dates)

    query = f"""
        SELECT DISTINCT ON (
            player_name,
            game_date
        )
            player_name,
            game_date,
            consensus_odds,
            consensus_implied_prob,
            line,
            checkpoint,
            books_count,
            book_keys,
            book_prices
        FROM provider_consensus_history
        WHERE provider = 'prop_line'
          AND sport_key = 'baseball_mlb'
          AND market_key = %s
          AND player_name IN ({player_placeholders})
          AND game_date BETWEEN %s::date AND %s::date
          AND line = %s
          AND outcome_name = 'Over'
          AND checkpoint IN ('close', 'open')
        ORDER BY
            player_name,
            game_date,
            CASE checkpoint
                WHEN 'close' THEN 1
                WHEN 'open' THEN 2
                ELSE 99
            END
    """

    params = (
        [market.key]
        + requested_players
        + [min_date, max_date, line]
    )

    try:
        df = read_sql(query, params)

    except Exception:
        app.logger.exception(
            "Historical consensus lookup failed "
            "market=%s line=%s players=%s dates=%s",
            market.key,
            line,
            requested_players,
            visible_dates,
        )
        return {}

    if df.empty:
        return {}

    lookup = {}

    for _, row in df.iterrows():

        player = clean_text(row.get("player_name"))
        game_date = clean_text(row.get("game_date"))

        odds_value = row.get("consensus_odds")
        implied_prob = row.get("consensus_implied_prob")
        books_count = row.get("books_count")

        try:
            odds_value = int(odds_value)
        except (TypeError, ValueError):
            odds_value = None

        try:
            implied_prob = float(implied_prob)
        except (TypeError, ValueError):
            implied_prob = None

        try:
            books_count = int(books_count)
        except (TypeError, ValueError):
            books_count = 0

        line_value = row.get("line")

        if line_value is not None and not pd.isna(line_value):
            line_value = float(line_value)
        else:
            line_value = None

        lookup[(player, game_date)] = {
            "odds": odds_value,

            # Keep the existing template interface.
            "sportsbook": (
                f"AVG {books_count} BOOK"
                f"{'' if books_count == 1 else 'S'}"
            ),

            "line": line_value,
            "checkpoint": clean_text(row.get("checkpoint")),
            "implied_prob": implied_prob,

            "book_count": books_count,
            "books": row.get("book_keys"),
            "book_prices": row.get("book_prices"),
        }

    return lookup

def build_compare_result(players, role, source_df, prop, window, mode, line, min_value, max_value, ftext, weekday="all"):
    summaries = []
    rows_by_player = {}

    # Build the visible stat rows first. This gives the odds query the exact
    # game dates it needs instead of searching the full historical table.
    for player_name in players:
        if role == "hitter":
            rows = build_hitter_game_rows(
                source_df,
                player_name,
                prop,
                window,
                mode,
                line,
                min_value,
                max_value,
                weekday,
            )
        else:
            rows = build_pitcher_game_rows(
                source_df,
                player_name,
                prop,
                window,
                mode,
                line,
                min_value,
                max_value,
            )

        rows_by_player[player_name] = (
            rows.set_index("game_date")
            if not rows.empty
            else rows
        )

    all_dates = sorted(
        set().union(*[
            set(rows.index.tolist())
            for rows in rows_by_player.values()
            if not rows.empty
        ]),
        reverse=True,
    )[:window]

    odds_lookup = get_historical_odds_lookup(
        players,
        prop,
        line,
        role=role,
        game_dates=all_dates,
    )

    for player_name in players:
        rows_indexed = rows_by_player[player_name]

        if rows_indexed.empty:
            rows = rows_indexed
        else:
            rows = rows_indexed.reset_index()

        summary = summarize_player(
            player_name,
            rows,
            prop,
            window,
            mode,
            line,
            min_value,
            max_value,
            ftext,
        )

        player_odds = [
            (d, v)
            for (p, d), v in odds_lookup.items()
            if p == player_name
        ]

        if player_odds:
            _, latest_odds = max(
                player_odds,
                key=lambda item: item[0],
            )
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
                    "sportsbook": odds_data.get("sportsbook"),
                    "odds_checkpoint": odds_data.get("checkpoint"),
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

# ---------------------------------------------------------
# Registry-driven market options used by site features.
#
# ui_value keeps compatibility with the prop names already
# used throughout HitRateHub URLs/templates.
# ---------------------------------------------------------
MARKET_UI_EXCLUDED = {
    "pitcher_earned_runs_allowed",
    "pitcher_walks_allowed",
}


def market_ui_value(market):
    if market.key == "batter_rbis":
        return "rbi"

    if market.key == "pitcher_strikeouts":
        return "strikeouts"

    if market.entity == "batter":
        return market.key.removeprefix("batter_")

    return market.key


def get_market_options(entity, sport="MLB"):
    options = []

    for market in MARKETS.values():
        if market.sport != sport:
            continue
        if market.entity != entity:
            continue
        if market.key in MARKET_UI_EXCLUDED:
            continue

        options.append({
            "value": market_ui_value(market),
            "market_key": market.key,
            "display": market.display,
        })

    return options


def thresholds_for(role, prop):
    market = resolve_market(display_prop=prop)

    market_key = market.key if market else ""

    if role == "pitcher":
        if market_key == "pitcher_outs":
            return [14.5, 15.5, 16.5, 17.5, 18.5]
        if market_key == "pitcher_strikeouts":
            return [3.5, 4.5, 5.5, 6.5, 7.5, 8.5]
        if market_key in {
            "pitcher_earned_runs",
            "pitcher_earned_runs_allowed",
            "pitcher_hits_allowed",
            "pitcher_walks",
            "pitcher_walks_allowed",
            "pitcher_runs_allowed",
        }:
            return [0.5, 1.5, 2.5, 3.5, 4.5]

        return [0.5, 1.5, 2.5, 3.5, 4.5]

    if market_key in {
        "batter_home_runs",
        "batter_stolen_bases",
    }:
        return [0.5, 1.5]

    if market_key in {
        "batter_hits",
        "batter_singles",
        "batter_doubles",
        "batter_runs",
        "batter_rbis",
        "batter_walks",
    }:
        return [0.5, 1.5, 2.5, 3.5]

    if market_key == "batter_hits_runs_rbis":
        return [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]

    if market_key == "batter_total_bases":
        return [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]

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
        df.groupby(
            ["batter_name", "game_date"],
            as_index=False
        )
        .agg({
            "team": "last",
            "h": "sum",
            "single": "sum",
            "double": "sum",
            "tb": "sum",
            "hr": "sum",
            "bb": "sum",
            "sb": "sum",
            "runs_scored": "sum",
            "rbi": "sum",
        })
        .sort_values(
            ["batter_name", "game_date"],
            ascending=[True, False]
        )
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

    day_night = clean_text(request.args.get("day_night", "")).lower()
    
    selected_weekday = request.args.get("weekday", "all")

    if selected_weekday not in ("all", "0", "1", "2", "3", "4", "5", "6"):
        selected_weekday = "all"

    if vs_hand not in ("", "R", "L"):
        vs_hand = ""

    if day_night not in ("", "day", "night"):
        day_night = ""

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
    ftext = filter_text(vs_team, vs_hand, selected_weekday, day_night)
      
    try:
        # On the calculator, only load the player-name list for the
        # currently selected role. This avoids fetching and serializing
        # two full MLB player lists on every compare request.
        if active_page == "calculator":
            if calc_role == "hitter":
                hitter_names = get_hitter_names()
                pitcher_names = []
            else:
                hitter_names = []
                pitcher_names = get_pitcher_names()
        else:
            hitter_names = get_hitter_names()
            pitcher_names = get_pitcher_names()

        lineup_map = get_today_lineups()

        # Weather is only needed for pages that display
        # weather-enhanced leaderboard data.
        if active_page == "leaderboard":
            try:
                weather_lookup = load_team_weather()
            except Exception as e:
                print("Weather lookup error:", e)
                weather_lookup = {}
        else:
            weather_lookup = {}
        selected_players = []

        for i in range(1, 11):
            field_name = "calc_player" if i == 1 else f"calc_compare_{i}"
            player_name = clean_text(request.args.get(field_name, ""))

            if player_name and player_name not in selected_players:
                selected_players.append(player_name)

        pa_df = pd.DataFrame()
        pitcher_df = pd.DataFrame()

        if calc_role == "hitter" or role == "hitter":
            if active_page == "calculator" and selected_players:
                pa_df_raw = get_pa_data_for_players(selected_players)
            else:
                pa_df_raw = get_pa_data()

            teams = get_teams_from_pa(pa_df_raw)
            pa_df = filter_hitter_df(
                pa_df_raw,
                vs_team,
                vs_hand,
                day_night
            )

            if active_page in ("leaderboard", "trends"):
                pa_df = apply_lineup_filter(
                    pa_df,
                    lineup_map,
                    lineup_filter
                )

        if calc_role == "pitcher" or role == "pitcher":
            pitcher_df = get_pitcher_data()

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
                        calc_max,
                        selected_weekday
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
                        ftext,
                        selected_weekday
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

        # Only build the full leaderboard when the user
        # is actually on the leaderboard page.
        if active_page == "leaderboard":
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

                    item["weather_display"] = weather.get(
                        "weather_display", ""
                    )
                    item["opp_pitcher"] = weather.get(
                        "opp_pitcher", ""
                    )
                    item["opp_pitcher_hand"] = weather.get(
                        "opp_pitcher_hand", ""
                    )

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
        "hitter_market_options": get_market_options("batter"),
        "pitcher_market_options": get_market_options("pitcher"),
        "teams": teams,
        "vs_team": vs_team,
        "vs_hand": vs_hand,
        "day_night": day_night,
        "selected_weekday": selected_weekday,
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
    hitter_market_options = get_market_options("batter")
    valid_props = {option["value"] for option in hitter_market_options}

    if prop not in valid_props:
        prop = "hits"

    thresholds = thresholds_for("hitter", prop)
    trend_line = safe_float(
        request.args.get("trend_line", thresholds[0]),
        thresholds[0],
    )

    if trend_line not in thresholds:
        trend_line = thresholds[0]

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
            "single": "sum",
            "double": "sum",
            "tb": "sum",
            "hr": "sum",
            "bb": "sum",
            "sb": "sum",
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

        hits = int((recent["stat_value"] > trend_line).sum())
        games = len(recent)
        rate = round((hits / games) * 100, 1)
        avg = round(float(recent["stat_value"].mean()), 2)

        streak = 0

        for _, row in recent.iterrows():
            if row["stat_value"] > trend_line:
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

        recent_rate = (recent_half["stat_value"] > trend_line).mean() * 100
        older_rate = (older_half["stat_value"] > trend_line).mean() * 100
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
        "trend_line": trend_line,
        "trend_thresholds": thresholds,
        "hitter_market_options": hitter_market_options,
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
    combo_line = safe_float(request.args.get("combo_line", 0.5), 0.5)

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
              AND line = %s
            ORDER BY rate DESC, games DESC, edge DESC
            LIMIT %s
        """, (
            combo_prop,
            combo_size,
            combo_window,
            combo_line,
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
        "combo_line": combo_line,
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
        CASE
            WHEN MIN(pa_number) = 1 THEN 1
            WHEN MIN(pa_number) = 2 THEN 2
            WHEN MIN(pa_number) = 3 THEN 3
            WHEN MIN(pa_number) = 4 THEN 4
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
@app.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect("/")

    state = secrets.token_urlsafe(32)
    session["discord_oauth_state"] = state

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state
    }

    discord_authorize_url = (
        "https://discord.com/oauth2/authorize?"
        + urlencode(params)
    )

    return redirect(discord_authorize_url)


@app.route("/auth/discord/callback")
def discord_callback():
    if request.args.get("error"):
        return redirect("/")

    returned_state = request.args.get("state")
    saved_state = session.pop("discord_oauth_state", None)

    if not saved_state or returned_state != saved_state:
        return "Invalid Discord login state.", 400

    code = request.args.get("code")

    if not code:
        return "Discord did not return an authorization code.", 400

    try:
        token_response = requests.post(
            f"{DISCORD_API_URL}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            timeout=15
        )

        token_response.raise_for_status()
        token_data = token_response.json()

        access_token = token_data.get("access_token")

        if not access_token:
            return "Discord did not return an access token.", 400

        user_response = requests.get(
            f"{DISCORD_API_URL}/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=15
        )

        user_response.raise_for_status()
        discord_user = user_response.json()

        discord_id = str(discord_user["id"])
        username = (
            discord_user.get("global_name")
            or discord_user.get("username")
            or "Discord User"
        )

        avatar_hash = discord_user.get("avatar")

        if avatar_hash:
            avatar_url = (
                f"https://cdn.discordapp.com/avatars/"
                f"{discord_id}/{avatar_hash}.png?size=128"
            )
        else:
            avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

        # ---------------------------------------------------------
        # Verify the Discord user is actually in HitRateHub
        # ---------------------------------------------------------
        if not DISCORD_GUILD_ID or not DISCORD_BOT_TOKEN:
            print("Discord guild verification is not configured.")
            return "Discord server verification is unavailable.", 500

        member_response = requests.get(
            f"{DISCORD_API_URL}/guilds/{DISCORD_GUILD_ID}/members/{discord_id}",
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
            },
            timeout=15
        )

        if member_response.status_code == 404:
            return render_template(
                "join_discord.html",
                discord_invite_url=DISCORD_INVITE_URL
            ), 403

        member_response.raise_for_status()

        conn = get_conn()

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (
                            discord_id,
                            username,
                            avatar,
                            updated_at
                        )
                        VALUES (%s, %s, %s, NOW())

                        ON CONFLICT (discord_id)
                        DO UPDATE SET
                            username = EXCLUDED.username,
                            avatar = EXCLUDED.avatar,
                            updated_at = NOW()

                        RETURNING
                            id,
                            discord_id,
                            username,
                            avatar,
                            membership_tier,
                            community_status,
                            is_beta_tester,
                            is_admin
                    """, (
                        discord_id,
                        username,
                        avatar_url
                    ))

                    row = cur.fetchone()

        finally:
            conn.close()

        user = User(
            id=row[0],
            discord_id=row[1],
            username=row[2],
            avatar=row[3],
            membership_tier=row[4],
            community_status=row[5],
            is_beta_tester=row[6],
            is_admin=row[7]
        )
        login_user(user, remember=True)

        return redirect("/")

    except requests.RequestException as e:
        print("Discord OAuth request error:", e)
        return "Discord login failed. Please try again.", 500

    except Exception as e:
        print("Discord login error:", e)
        return "Unable to complete Discord login.", 500

@app.route("/account")
@login_required
def account_page():
    return render_template(
        "account.html",
        active_page="account"
    )

@app.route("/logout")
def logout():
    logout_user()
    session.pop("discord_oauth_state", None)
    return redirect(url_for("index"))

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
                "timeline_updated": 0,
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

        timeline_sql = """
            INSERT INTO odds_market_timeline (
                game_date,
                checkpoint,
                player,
                sportsbook,
                prop,
                ou,
                line,
                odds,
                gameid,
                home,
                away,
                starttime,
                source_captured_at
            )
            SELECT DISTINCT ON (DATE(starttime), player, sportsbook, prop, ou, line)
                DATE(starttime) AS game_date,
                'close' AS checkpoint,
                player,
                sportsbook,
                prop,
                ou,
                line,
                odds,
                gameid,
                home,
                away,
                starttime,
                NOW() AS source_captured_at
            FROM odds_last_seen
            WHERE seen_run_id = %s
              AND odds IS NOT NULL
              AND line IS NOT NULL
              AND player IS NOT NULL
              AND prop IS NOT NULL
              AND LOWER(ou) = 'over'
              AND COALESCE(ismain, 1) = 1
            ORDER BY DATE(starttime), player, sportsbook, prop, ou, line, updated_at DESC
            ON CONFLICT (game_date, checkpoint, player, sportsbook, prop, ou, line)
            DO UPDATE SET
                odds = EXCLUDED.odds,
                gameid = EXCLUDED.gameid,
                home = EXCLUDED.home,
                away = EXCLUDED.away,
                starttime = EXCLUDED.starttime,
                source_captured_at = EXCLUDED.source_captured_at
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

        conn = get_conn()

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.executemany(snapshot_sql, clean_rows)
                    cur.executemany(last_seen_sql, clean_rows)

                    timeline_count = 0
                    closed_count = 0
                    deleted_count = 0
                   
                     # <-- ALWAYS update the timeline
                    cur.execute(timeline_sql, (run_id,))
                    timeline_count = cur.rowcount

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
                "timeline_updated": timeline_count,
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

@app.route("/edge-finder")
def edge_finder():
    prop = request.args.get("prop", "HR")
    view = request.args.get("view", "overpriced")
    sort_by = request.args.get("sort", "edge_l20")
    selected_date = request.args.get("date", "today")

    eastern_now = datetime.now(ZoneInfo("America/Toronto"))
    today = eastern_now.date()
    yesterday = today - timedelta(days=1)

    if selected_date == "today":
        filter_date = today
    elif selected_date == "yesterday":
        filter_date = yesterday
    else:
        filter_date = selected_date

    conn = get_conn()

    sql = """
        SELECT *
        FROM edge_finder_cache
        WHERE prop = %s
          AND snapshot_date = %s
    """

    params = [prop, filter_date]

    if view == "overpriced":
        sql += " AND edge_l20 >= 5 "
    elif view == "underpriced":
        sql += " AND edge_l20 <= -5 "

    allowed_sorts = {
        "edge_l10": "edge_l10",
        "edge_l20": "edge_l20",
        "edge_l30": "edge_l30",
        "edge_l45": "edge_l45",
        "implied_prob": "implied_prob",
        "odds": "odds"
    }

    sort_col = allowed_sorts.get(sort_by, "edge_l20")

    if view == "underpriced":
        sql += f" ORDER BY {sort_col} ASC LIMIT 100"
    else:
        sql += f" ORDER BY {sort_col} DESC LIMIT 100"

    df = pd.read_sql(sql, conn, params=params)
    conn.close()

    rows = df.to_dict("records")

    return render_template(
        "edge_finder.html",
        rows=rows,
        prop=prop,
        view=view,
        sort_by=sort_by,
        selected_date=selected_date,
        active_page="edge_finder"
    )

@app.route("/")
def index():
    # Render repeatedly probes the root with HEAD requests.
    # Do not build the full calculator context for those checks.
    if request.method == "HEAD":
        return "", 200

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

@app.route("/systems")
@login_required
def systems_page():

    systems = get_systems()

    return render_template(
        "systems.html",
        active_page="systems",
        systems=systems.to_dict("records")
    )

@app.route("/systems/<system_code>")
@login_required
def system_detail_page(system_code):

    system_df = read_sql("""
        SELECT
            s.id,
            s.system_code,
            s.name,
            s.description,
            s.creator_id,
            s.sport,
            s.visibility,
            s.status,
            COUNT(sf.id) AS followers,
            s.created_at,
            s.updated_at
        FROM systems s
        LEFT JOIN system_followers sf
            ON sf.system_id = s.id
        WHERE s.system_code = %s
        GROUP BY
            s.id,
            s.system_code,
            s.name,
            s.description,
            s.creator_id,
            s.sport,
            s.visibility,
            s.status,
            s.created_at,
            s.updated_at
        LIMIT 1
    """, (system_code,))

    if system_df.empty:
        return render_template(
            "404.html",
            active_page="systems"
        ), 404

    system = system_df.iloc[0].to_dict()

    is_watching = is_watching_system(
        current_user.id,
        system["id"]
    )

    # Load the saved combo connected to this system.
    combo_df = read_sql("""
        SELECT
            id,
            system_id,
            combo_name,
            leg_count,
            minimum_combined_odds,
            require_all_active,
            require_exact_lines,
            created_at
        FROM system_combos
        WHERE system_id = %s
        ORDER BY id ASC
        LIMIT 1
    """, (system["id"],))

    combo = None
    combo_legs = []

    if not combo_df.empty:
        combo = combo_df.iloc[0].to_dict()

        legs_df = read_sql("""
            SELECT
                id,
                combo_id,
                player_name,
                prop,
                ou,
                line,
                sort_order
            FROM system_combo_legs
            WHERE combo_id = %s
            ORDER BY sort_order ASC, id ASC
        """, (combo["id"],))

        if not legs_df.empty:
            combo_legs = legs_df.to_dict("records")
    qualifier_result = None

    if combo and combo_legs:
        try:
            qualifier_result = check_saved_system(
                combo,
                combo_legs,
                preferred_sportsbook="fanduel"
            )

        except Exception as e:
            app.logger.exception(
                "Today's system qualifier failed for %s",
                system_code
            )

            qualifier_result = {
                "status": "error",
                "qualified": False,
                "message": "Today's check could not be completed.",
                "combined_odds": None,
                "minimum_required": (
                    combo.get("minimum_combined_odds")
                    if combo
                    else None
                ),
                "odds_passed": None,
                "all_legs_passed": False,
                "legs": []
            }

    return render_template(
        "system_detail.html",
        active_page="systems",
        system=system,
        is_watching=is_watching,
        combo=combo,
        combo_legs=combo_legs,
        qualifier_result=qualifier_result
    )

@app.route(
    "/systems/<system_code>/ticket",
    methods=["GET", "POST"]
)
@login_required
def system_ticket_page(system_code):

    # Add ?preview=1 to the URL to use fake test odds.
    preview_mode = request.args.get("preview") == "1"

    # =========================================================
    # LOAD SYSTEM
    # =========================================================
    system_df = read_sql("""
        SELECT
            id,
            system_code,
            name,
            description,
            sport,
            visibility,
            status
        FROM systems
        WHERE system_code = %s
        LIMIT 1
    """, (system_code,))

    if system_df.empty:
        return render_template(
            "404.html",
            active_page="systems"
        ), 404

    system = system_df.iloc[0].to_dict()

    # =========================================================
    # LOAD COMBO
    # =========================================================
    combo_df = read_sql("""
        SELECT
            id,
            system_id,
            combo_name,
            leg_count,
            minimum_combined_odds,
            require_all_active,
            require_exact_lines,
            created_at
        FROM system_combos
        WHERE system_id = %s
        ORDER BY id ASC
        LIMIT 1
    """, (system["id"],))

    if combo_df.empty:
        flash(
            "This system does not have a saved combo.",
            "error"
        )

        return redirect(url_for(
            "system_detail_page",
            system_code=system_code
        ))

    combo = combo_df.iloc[0].to_dict()

    # =========================================================
    # LOAD COMBO LEGS
    # =========================================================
    legs_df = read_sql("""
        SELECT
            id,
            combo_id,
            player_name,
            prop,
            ou,
            line,
            sort_order
        FROM system_combo_legs
        WHERE combo_id = %s
        ORDER BY sort_order ASC, id ASC
    """, (combo["id"],))

    combo_legs = legs_df.to_dict("records")

    if not combo_legs:
        flash(
            "This saved combo does not contain any legs.",
            "error"
        )

        return redirect(url_for(
            "system_detail_page",
            system_code=system_code
        ))

    # =========================================================
    # CHECK REAL QUALIFICATION
    # =========================================================
    qualifier_result = check_saved_system(
        combo,
        combo_legs,
        preferred_sportsbook="fanduel"
    )

    # =========================================================
    # CREATE FAKE TICKET WHEN PREVIEW MODE IS ACTIVE
    # =========================================================
    if preview_mode:

        preview_odds = [
            150,
            175,
            200,
            225,
            250
        ]

        preview_legs = []

        for index, saved_leg in enumerate(combo_legs):

            fake_odds = preview_odds[
                index % len(preview_odds)
            ]

            preview_legs.append({
                "player_name": saved_leg["player_name"],
                "prop": saved_leg["prop"],
                "ou": saved_leg["ou"],
                "current_line": saved_leg["line"],
                "current_odds": fake_odds,
                "sportsbook": "fanduel",
                "game_id": f"preview-{index + 1}",
                "home_team": "TOR",
                "away_team": "NYY",
                "start_time": None,
                "last_update": None
            })

        # Calculate fake combined American odds.
        combined_decimal = 1.0

        for leg in preview_legs:

            odds = float(leg["current_odds"])

            if odds > 0:
                decimal_odds = 1 + (odds / 100)
            else:
                decimal_odds = 1 + (
                    100 / abs(odds)
                )

            combined_decimal *= decimal_odds

        if combined_decimal >= 2:
            preview_combined_odds = round(
                (combined_decimal - 1) * 100
            )
        else:
            preview_combined_odds = round(
                -100 / (combined_decimal - 1)
            )

        qualifier_result = {
            "qualified": True,
            "status": "preview",
            "message": "Development preview ticket",
            "sportsbook": "fanduel",
            "combined_odds": preview_combined_odds,
            "minimum_required": combo.get(
                "minimum_combined_odds"
            ),
            "odds_passed": True,
            "all_legs_passed": True,
            "legs": preview_legs
        }

    # =========================================================
    # BLOCK NORMAL PAGE WHEN SYSTEM IS NOT QUALIFIED
    # =========================================================
    elif not qualifier_result.get("qualified"):

        flash(
            "This system is not currently qualified.",
            "warning"
        )

        return redirect(url_for(
            "system_detail_page",
            system_code=system_code
        ))

    # =========================================================
    # LOAD USER BANKROLL
    # =========================================================
    bankroll_df = read_sql("""
        SELECT
            id,
            name,
            starting_balance,
            current_balance,
            unit_percentage,
            auto_resize,
            is_default
        FROM user_bankrolls
        WHERE user_id = %s
        ORDER BY
            is_default DESC,
            id ASC
        LIMIT 1
    """, (current_user.id,))

    if bankroll_df.empty:

        selected_bankroll_id = None
        selected_bankroll_name = "Main Bankroll"
        current_bankroll = 1000.00
        unit_percentage = 0.01
        auto_resize = True

    else:

        bankroll = bankroll_df.iloc[0].to_dict()

        selected_bankroll_id = bankroll["id"]
        selected_bankroll_name = bankroll["name"]

        current_bankroll = float(
            bankroll["current_balance"]
        )

        unit_percentage = float(
            bankroll["unit_percentage"]
        )

        auto_resize = bool(
            bankroll["auto_resize"]
        )
        # ============================================================
    # SAVE TICKET
    # ============================================================
    if request.method == "POST":

        print("SAVE DEBUG: POST reached")
        print("SAVE DEBUG: preview_mode =", preview_mode)

        # Preview blocking temporarily disabled for testing.
        # if preview_mode:
        #     flash(
        #         "Preview tickets cannot be added to the tracker.",
        #         "error"
        #     )
        #
        #     return redirect(url_for(
        #         "system_ticket_page",
        #         system_code=system_code,
        #         preview=1
        #     ))

        # Only recheck qualification for real tickets.
        if not preview_mode:
            qualifier_result = check_saved_system(
                combo,
                combo_legs,
                preferred_sportsbook="fanduel"
            )

            if not qualifier_result.get("qualified"):
                flash(
                    "This system no longer qualifies. The ticket was not saved.",
                    "warning"
                )

                return redirect(url_for(
                    "system_detail_page",
                    system_code=system_code
                ))

        print("SAVE DEBUG: reached database save block")

    # Your stake/form validation and database save code continues here.

        # -----------------------------------------------------
        # Validate stake and units
        # -----------------------------------------------------
        try:
            stake = round(float(request.form.get("stake", 0)), 2)
            units = round(float(request.form.get("units", 0)), 4)
        except (TypeError, ValueError):
            flash(
                "Stake and units must be valid numbers.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        if stake <= 0:
            flash(
                "Stake must be greater than $0.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        if units <= 0:
            flash(
                "Units must be greater than zero.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        if stake > 1000000:
            flash(
                "The entered stake is too large.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        # -----------------------------------------------------
        # Validate the selected bankroll
        # -----------------------------------------------------
        posted_bankroll_id = clean_text(
            request.form.get("bankroll_id")
        )

        bankroll_id = None
        bankroll_balance = 1000.00
        saved_unit_percentage = 0.01

        if posted_bankroll_id:
            try:
                posted_bankroll_id = int(posted_bankroll_id)
            except (TypeError, ValueError):
                flash(
                    "The selected bankroll is invalid.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            owned_bankroll_df = read_sql("""
                SELECT
                    id,
                    current_balance,
                    unit_percentage
                FROM user_bankrolls
                WHERE id = %s
                  AND user_id = %s
                LIMIT 1
            """, (
                posted_bankroll_id,
                current_user.id
            ))

            if owned_bankroll_df.empty:
                flash(
                    "You do not have access to that bankroll.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            owned_bankroll = (
                owned_bankroll_df
                .iloc[0]
                .to_dict()
            )

            bankroll_id = int(owned_bankroll["id"])

            bankroll_balance = round(
                float(owned_bankroll["current_balance"]),
                2
            )

            saved_unit_percentage = float(
                owned_bankroll["unit_percentage"]
            )

        # -----------------------------------------------------
        # Validate selected unit percentage
        # -----------------------------------------------------
        posted_unit_percentage = clean_text(
            request.form.get("unit_percentage")
        )

        if posted_unit_percentage == "custom":
            try:
                custom_percent = float(
                    request.form.get(
                        "custom_unit_percentage",
                        0
                    )
                )

                unit_percentage = (
                    custom_percent / 100
                )

            except (TypeError, ValueError):
                unit_percentage = saved_unit_percentage

        else:
            try:
                unit_percentage = float(
                    posted_unit_percentage
                )
            except (TypeError, ValueError):
                unit_percentage = saved_unit_percentage

        if unit_percentage <= 0 or unit_percentage > 1:
            flash(
                "The selected unit percentage is invalid.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        unit_value = round(
            bankroll_balance * unit_percentage,
            2
        )

        # -----------------------------------------------------
        # Build official and user-entered legs
        # -----------------------------------------------------
        official_legs = qualifier_result.get("legs", [])

        if not official_legs:
            flash(
                "No qualified legs were available to save.",
                "error"
            )

            return redirect(url_for(
                "system_detail_page",
                system_code=system_code
            ))

        user_legs = []
        user_decimal_total = 1.0

        allowed_sportsbooks = {
            "fanduel",
            "draftkings",
            "bet365",
            "betmgm",
            "caesars",
            "betrivers",
            "pointsbet",
            "other"
        }

        for index, official_leg in enumerate(
            official_legs,
            start=1
        ):
            official_odds = official_leg.get(
                "current_odds"
            )

            official_line = official_leg.get(
                "current_line"
            )

            if official_odds is None:
                flash(
                    "One of the official ticket legs no longer has odds.",
                    "error"
                )

                return redirect(url_for(
                    "system_detail_page",
                    system_code=system_code
                ))

            try:
                official_odds = int(official_odds)
            except (TypeError, ValueError):
                flash(
                    "One of the official ticket odds is invalid.",
                    "error"
                )

                return redirect(url_for(
                    "system_detail_page",
                    system_code=system_code
                ))

            try:
                user_odds = int(
                    request.form.get(
                        f"leg_odds_{index}",
                        official_odds
                    )
                )
            except (TypeError, ValueError):
                flash(
                    f"Leg {index} has invalid odds.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            if user_odds == 0:
                flash(
                    f"Leg {index} odds cannot be zero.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            if user_odds < -100000 or user_odds > 100000:
                flash(
                    f"Leg {index} odds are outside the allowed range.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            user_sportsbook = clean_text(
                request.form.get(
                    f"leg_sportsbook_{index}",
                    official_leg.get("sportsbook")
                    or "fanduel"
                )
            ).lower()

            if user_sportsbook not in allowed_sportsbooks:
                user_sportsbook = "other"

            decimal_odds = american_to_decimal(
                user_odds
            )

            if decimal_odds is None:
                flash(
                    f"Leg {index} has invalid odds.",
                    "error"
                )

                return redirect(url_for(
                    "system_ticket_page",
                    system_code=system_code
                ))

            user_decimal_total *= decimal_odds

            user_legs.append({
                "player_name": clean_text(
                    official_leg.get("player_name")
                ),
                "prop": normalize_system_prop(
                    official_leg.get("prop")
                ),
                "ou": clean_text(
                    official_leg.get("ou")
                ).lower(),
                "official_line": official_line,
                "user_line": official_line,
                "official_odds": official_odds,
                "user_odds": user_odds,
                "official_sportsbook": clean_text(
                    official_leg.get("sportsbook")
                    or "fanduel"
                ).lower(),
                "user_sportsbook": user_sportsbook,
                "game_id": clean_text(
                    official_leg.get("game_id")
                ) or None,
                "home_team": clean_text(
                    official_leg.get("home_team")
                ) or None,
                "away_team": clean_text(
                    official_leg.get("away_team")
                ) or None,
                "start_time": official_leg.get(
                    "start_time"
                )
            })

        official_combined_odds = int(
            qualifier_result["combined_odds"]
        )

        user_combined_odds = decimal_to_american(
            user_decimal_total
        )

        if user_combined_odds is None:
            flash(
                "The entered ticket odds could not be calculated.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        user_combined_odds = int(
            user_combined_odds
        )

        # Personal payout always uses the user's entered odds.
        user_decimal_odds = american_to_decimal(
            user_combined_odds
        )

        potential_return = round(
            stake * user_decimal_odds,
            2
        )

        potential_profit = round(
            potential_return - stake,
            2
        )
        # -----------------------------------------------------
        # Find/create user-system record and save full ticket
        # -----------------------------------------------------
        ticket_id = str(uuid.uuid4())

        ticket_sportsbook = clean_text(
            request.form.get(
                "ticket_sportsbook",
                "fanduel"
            )
        ).lower()

        if ticket_sportsbook not in allowed_sportsbooks:
            ticket_sportsbook = "other"

        conn = get_conn()

        try:
            with conn:
                with conn.cursor() as cur:

                    # -----------------------------------------
                    # Find the user's existing system record
                    # -----------------------------------------
                    cur.execute("""
                        SELECT id
                        FROM user_systems
                        WHERE user_id = %s
                          AND system_id = %s
                        LIMIT 1
                    """, (
                        current_user.id,
                        system["id"]
                    ))

                    user_system_row = cur.fetchone()

                    if user_system_row:
                        user_system_id = user_system_row[0]

                        cur.execute("""
                            UPDATE user_systems
                            SET
                                betting = TRUE,
                                started_betting = COALESCE(
                                    started_betting,
                                    NOW()
                                )
                            WHERE id = %s
                        """, (
                            user_system_id,
                        ))

                    else:
                        cur.execute("""
                            INSERT INTO user_systems (
                                user_id,
                                system_id,
                                watching,
                                betting,
                                favorite,
                                notifications,
                                started_betting
                            )
                            VALUES (
                                %s,
                                %s,
                                FALSE,
                                TRUE,
                                FALSE,
                                FALSE,
                                NOW()
                            )
                            RETURNING id
                        """, (
                            current_user.id,
                            system["id"]
                        ))

                        user_system_id = cur.fetchone()[0]

                    # -----------------------------------------
                    # Save main ticket
                    # -----------------------------------------
                    cur.execute("""
                        INSERT INTO user_bets (
                            user_system_id,
                            sportsbook,
                            odds_taken,
                            stake,
                            units,
                            result,
                            profit,
                            bet_time,
                            created_at,
                            ticket_id,
                            bet_type,
                            combined_odds,
                            potential_profit,
                            potential_return,
                            status,
                            user_id,
                            unit_value,
                            bankroll_id,
                            official_combined_odds,
                            user_combined_odds,
                            bankroll_balance_at_bet,
                            unit_percentage
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            NULL,
                            NULL,
                            NOW(),
                            NOW(),
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s
                        )
                        RETURNING id
                    """, (
                        user_system_id,
                        ticket_sportsbook,
                        user_combined_odds,
                        stake,
                        units,
                        ticket_id,
                        "parlay",
                        user_combined_odds,
                        potential_profit,
                        potential_return,
                        "pending",
                        current_user.id,
                        unit_value,
                        bankroll_id,
                        official_combined_odds,
                        user_combined_odds,
                        bankroll_balance,
                        unit_percentage
                    ))

                    user_bet_id = cur.fetchone()[0]

                    # -----------------------------------------
                    # Save every ticket leg
                    # -----------------------------------------
                    for leg in user_legs:
                        cur.execute("""
                            INSERT INTO user_bet_legs (
                                ticket_id,
                                user_bet_id,
                                player_name,
                                prop,
                                ou,
                                line,
                                odds,
                                sportsbook,
                                game_id,
                                home_team,
                                away_team,
                                start_time,
                                status,
                                created_at,
                                official_sportsbook,
                                official_odds,
                                user_sportsbook,
                                user_odds,
                                official_line,
                                user_line
                            )
                            VALUES (
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                NOW(),
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s
                            )
                        """, (
                            ticket_id,
                            user_bet_id,
                            leg["player_name"],
                            leg["prop"],
                            leg["ou"],
                            leg["user_line"],
                            leg["user_odds"],
                            leg["user_sportsbook"],
                            leg["game_id"],
                            leg["home_team"],
                            leg["away_team"],
                            leg["start_time"],
                            "pending",
                            leg["official_sportsbook"],
                            leg["official_odds"],
                            leg["user_sportsbook"],
                            leg["user_odds"],
                            leg["official_line"],
                            leg["user_line"]
                        ))

        except Exception:
            app.logger.exception(
                "Ticket save failed for system %s and user %s",
                system_code,
                current_user.id
            )

            flash(
                "The ticket could not be saved. Nothing was added.",
                "error"
            )

            return redirect(url_for(
                "system_ticket_page",
                system_code=system_code
            ))

        finally:
            conn.close()

        flash(
            "Ticket added to your Bet Tracker.",
            "success"
        )

        return redirect(url_for(
            "system_detail_page",
            system_code=system_code
        ))
    # =========================================================
    # DISPLAY PAGE
    # =========================================================
    return render_template(
        "system_ticket.html",
        active_page="systems",
        system=system,
        combo=combo,
        ticket=qualifier_result,
        preview_mode=preview_mode,
        selected_bankroll_id=selected_bankroll_id,
        selected_bankroll_name=selected_bankroll_name,
        current_bankroll=current_bankroll,
        unit_percentage=unit_percentage,
        auto_resize=auto_resize
    )

def bankroll_limit_for_user(user):
    """
    Return the maximum number of bankrolls this user may own.

    None means unlimited.
    """
    if getattr(user, "is_admin", False):
        return None

    if (
        getattr(user, "is_beta_tester", False)
        or getattr(user, "is_premium_plus", False)
        or getattr(user, "has_capper_access", False)
    ):
        return 10

    if getattr(user, "is_premium", False):
        return 3

    return 1


def user_bankroll_count(user_id):
    df = read_sql("""
        SELECT COUNT(*) AS bankroll_count
        FROM user_bankrolls
        WHERE user_id = %s
    """, (user_id,))

    if df.empty:
        return 0

    return int(df.iloc[0]["bankroll_count"] or 0)


def normalize_unit_percentage(value, default=0.01):
    try:
        percentage = float(value)
    except (TypeError, ValueError):
        return default

    # UI accepts a percentage such as 1.0 for 1%.
    if percentage > 0.25:
        percentage = percentage / 100.0

    return min(max(percentage, 0.001), 0.25)


@app.route("/my-hub/bankrolls", methods=["GET"])
@login_required
def personal_bankrolls_api():
    bankrolls_df = read_sql("""
        SELECT
            id,
            name,
            starting_balance,
            current_balance,
            unit_percentage,
            auto_resize,
            is_default,
            created_at,
            updated_at
        FROM user_bankrolls
        WHERE user_id = %s
        ORDER BY is_default DESC, created_at ASC
    """, (current_user.id,))

    bankrolls = []

    if not bankrolls_df.empty:
        for row in bankrolls_df.to_dict("records"):
            bankrolls.append({
                "id": int(row["id"]),
                "name": row.get("name") or "Bankroll",
                "starting_balance": float(row.get("starting_balance") or 0),
                "current_balance": float(row.get("current_balance") or 0),
                "unit_percentage": float(row.get("unit_percentage") or 0.01),
                "auto_resize": bool(row.get("auto_resize")),
                "is_default": bool(row.get("is_default")),
            })

    limit = bankroll_limit_for_user(current_user)

    return jsonify({
        "bankrolls": bankrolls,
        "count": len(bankrolls),
        "limit": limit,
        "unlimited": limit is None,
        "can_create": limit is None or len(bankrolls) < limit,
    })


@app.route("/my-hub/bankrolls/create", methods=["POST"])
@login_required
def create_personal_bankroll():
    name = clean_text(request.form.get("name")) or "Main Bankroll"
    starting_balance = safe_float(
        request.form.get("starting_balance"),
        0
    )
    unit_percentage = normalize_unit_percentage(
        request.form.get("unit_percentage"),
        0.01
    )
    auto_resize = (
        clean_text(request.form.get("auto_resize")).lower()
        in {"1", "true", "yes", "on"}
    )

    if starting_balance is None or starting_balance < 0:
        flash("Starting balance must be zero or greater.", "error")
        return redirect(url_for("my_bets_page"))

    limit = bankroll_limit_for_user(current_user)
    current_count = user_bankroll_count(current_user.id)

    if limit is not None and current_count >= limit:
        flash(
            f"Your membership allows up to {limit} bankroll"
            f"{'' if limit == 1 else 's'}.",
            "error"
        )
        return redirect(url_for("my_bets_page"))

    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*)
            FROM user_bankrolls
            WHERE user_id = %s
        """, (current_user.id,))

        is_first = int(cur.fetchone()[0] or 0) == 0

        cur.execute("""
            INSERT INTO user_bankrolls (
                user_id,
                name,
                starting_balance,
                current_balance,
                unit_percentage,
                auto_resize,
                is_default,
                created_at,
                updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
            )
            RETURNING id
        """, (
            current_user.id,
            name[:100],
            starting_balance,
            starting_balance,
            unit_percentage,
            auto_resize,
            is_first,
        ))

        bankroll_id = int(cur.fetchone()[0])

        if not is_first and (
            clean_text(request.form.get("make_default")).lower()
            in {"1", "true", "yes", "on"}
        ):
            cur.execute("""
                UPDATE user_bankrolls
                SET is_default = FALSE,
                    updated_at = NOW()
                WHERE user_id = %s
                  AND id <> %s
            """, (current_user.id, bankroll_id))

            cur.execute("""
                UPDATE user_bankrolls
                SET is_default = TRUE,
                    updated_at = NOW()
                WHERE user_id = %s
                  AND id = %s
            """, (current_user.id, bankroll_id))

        conn.commit()
        flash("Bankroll created.", "success")

    except Exception as exc:
        conn.rollback()
        print("Create bankroll error:", exc)
        flash("Unable to create bankroll.", "error")

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))


@app.route(
    "/my-hub/bankrolls/<int:bankroll_id>/update",
    methods=["POST"]
)
@login_required
def update_personal_bankroll(bankroll_id):
    name = clean_text(request.form.get("name")) or "Bankroll"
    unit_percentage = normalize_unit_percentage(
        request.form.get("unit_percentage"),
        0.01
    )
    auto_resize = (
        clean_text(request.form.get("auto_resize")).lower()
        in {"1", "true", "yes", "on"}
    )
    make_default = (
        clean_text(request.form.get("make_default")).lower()
        in {"1", "true", "yes", "on"}
    )

    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT id
            FROM user_bankrolls
            WHERE id = %s
              AND user_id = %s
        """, (bankroll_id, current_user.id))

        if not cur.fetchone():
            flash("Bankroll not found.", "error")
            return redirect(url_for("my_bets_page"))

        cur.execute("""
            UPDATE user_bankrolls
            SET
                name = %s,
                unit_percentage = %s,
                auto_resize = %s,
                updated_at = NOW()
            WHERE id = %s
              AND user_id = %s
        """, (
            name[:100],
            unit_percentage,
            auto_resize,
            bankroll_id,
            current_user.id,
        ))

        if make_default:
            cur.execute("""
                UPDATE user_bankrolls
                SET is_default = (id = %s),
                    updated_at = NOW()
                WHERE user_id = %s
            """, (bankroll_id, current_user.id))

        conn.commit()
        flash("Bankroll updated.", "success")

    except Exception as exc:
        conn.rollback()
        print("Update bankroll error:", exc)
        flash("Unable to update bankroll.", "error")

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))


@app.route(
    "/my-hub/bankrolls/<int:bankroll_id>/delete",
    methods=["POST"]
)
@login_required
def delete_personal_bankroll(bankroll_id):
    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT
                id,
                is_default,
                (
                    SELECT COUNT(*)
                    FROM user_bets
                    WHERE bankroll_id = user_bankrolls.id
                ) AS bet_count
            FROM user_bankrolls
            WHERE id = %s
              AND user_id = %s
        """, (bankroll_id, current_user.id))

        row = cur.fetchone()

        if not row:
            flash("Bankroll not found.", "error")
            return redirect(url_for("my_bets_page"))

        _, was_default, bet_count = row

        if int(bet_count or 0) > 0:
            flash(
                "This bankroll has tracked bets and cannot be deleted.",
                "error"
            )
            return redirect(url_for("my_bets_page"))

        cur.execute("""
            DELETE FROM user_bankrolls
            WHERE id = %s
              AND user_id = %s
        """, (bankroll_id, current_user.id))

        if was_default:
            cur.execute("""
                UPDATE user_bankrolls
                SET is_default = TRUE,
                    updated_at = NOW()
                WHERE id = (
                    SELECT id
                    FROM user_bankrolls
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                )
            """, (current_user.id,))

        conn.commit()
        flash("Bankroll deleted.", "success")

    except Exception as exc:
        conn.rollback()
        print("Delete bankroll error:", exc)
        flash("Unable to delete bankroll.", "error")

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))


@app.route("/my-hub/bets")
@login_required
def my_bets_page():
    status_filter = clean_text(
        request.args.get("status", "all")
    ).lower()

    bet_type_filter = clean_text(
        request.args.get("bet_type", "all")
    ).lower()

    sport_filter = clean_text(
        request.args.get("sport", "all")
    ).lower()

    allowed_statuses = {
        "all",
        "pending",
        "won",
        "lost",
        "push",
        "void"
    }

    allowed_bet_types = {
        "all",
        "straight",
        "parlay"
    }

    if status_filter not in allowed_statuses:
        status_filter = "all"

    if bet_type_filter not in allowed_bet_types:
        bet_type_filter = "all"

    where_parts = ["ub.user_id = %s"]
    params = [current_user.id]

    if status_filter != "all":
        where_parts.append(
            "LOWER(COALESCE(ub.status, 'pending')) = %s"
        )
        params.append(status_filter)

    if bet_type_filter != "all":
        where_parts.append(
            "LOWER(COALESCE(ub.bet_type, 'straight')) = %s"
        )
        params.append(bet_type_filter)

    if sport_filter != "all":
        where_parts.append(
            "LOWER(COALESCE(ub.sport, '')) = %s"
        )
        params.append(sport_filter)

    where_sql = " AND ".join(where_parts)

    bets_df = read_sql(f"""
        SELECT
            ub.id,
            ub.ticket_id,
            ub.user_system_id,
            ub.bankroll_id,
            ub.title,
            ub.sport,
            ub.league,
            ub.source,
            ub.sportsbook,
            ub.bet_type,
            ub.odds_taken,
            ub.combined_odds,
            ub.user_combined_odds,
            ub.stake,
            ub.units,
            ub.unit_value,
            ub.potential_profit,
            ub.potential_return,
            ub.status,
            ub.result,
            ub.profit,
            ub.bet_time,
            ub.created_at,
            ub.settled_at,
            ub.notes,
            ub.is_manual,
            ub.verification_type,
            bk.name AS bankroll_name,
            s.name AS system_name,
            s.system_code
        FROM user_bets ub
        LEFT JOIN user_bankrolls bk
            ON bk.id = ub.bankroll_id
           AND bk.user_id = ub.user_id
        LEFT JOIN user_systems us
            ON us.id = ub.user_system_id
           AND us.user_id = ub.user_id
        LEFT JOIN systems s
            ON s.id = us.system_id
        WHERE {where_sql}
        ORDER BY
            COALESCE(ub.bet_time, ub.created_at) DESC,
            ub.id DESC
        LIMIT 500
    """, params)

    bet_ids = []

    if not bets_df.empty:
        bet_ids = [
            int(value)
            for value in bets_df["id"].dropna().tolist()
        ]

    legs_by_bet = {}

    if bet_ids:
        placeholders = ",".join(["%s"] * len(bet_ids))

        legs_df = read_sql(f"""
            SELECT
                id,
                user_bet_id,
                player_name,
                selection_name,
                selection_type,
                team_name,
                opponent,
                sport,
                league,
                prop,
                ou,
                line,
                odds,
                sportsbook,
                game_id,
                home_team,
                away_team,
                start_time,
                status,
                result,
                sort_order,
                official_sportsbook,
                official_odds,
                user_sportsbook,
                user_odds,
                official_line,
                user_line
            FROM user_bet_legs
            WHERE user_bet_id IN ({placeholders})
            ORDER BY
                user_bet_id,
                COALESCE(sort_order, id)
        """, bet_ids)

        for leg in legs_df.to_dict("records"):
            bet_id = int(leg["user_bet_id"])

            legs_by_bet.setdefault(
                bet_id,
                []
            ).append(leg)

    bets = []

    from datetime import datetime

    now_value = datetime.now(
        ZoneInfo("America/Toronto")
    ).replace(tzinfo=None)

    if not bets_df.empty:
        for bet in bets_df.to_dict("records"):

            bet_id = int(bet["id"])

            raw_title = bet.get("title")
            if pd.isna(raw_title) or clean_text(raw_title).lower() == "nan":
                bet["title"] = ""

            bet["legs"] = legs_by_bet.get(bet_id, [])
            bet["leg_count"] = len(bet["legs"])

            event_starts = [
                leg["start_time"]
                for leg in bet["legs"]
                if leg.get("start_time")
            ]

            event_start = min(event_starts) if event_starts else None

            if event_start and getattr(event_start, "tzinfo", None):
                event_start = event_start.replace(tzinfo=None)

            bet["event_start"] = event_start

            bet["can_edit"] = (
                str(bet.get("status") or "pending").lower() == "pending"
                and (
                    event_start is None
                    or now_value < event_start
                )
            )

            bets.append(bet)

    summary_df = read_sql("""
        SELECT
            COUNT(*) AS total_bets,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, 'pending')) = 'pending'
            ) AS pending_bets,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'push'
            ) AS pushes,

            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) <> 'pending'
                    THEN profit
                    ELSE 0
                END
            ), 0) AS total_profit,

            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) <> 'pending'
                    THEN units
                    ELSE 0
                END
            ), 0) AS settled_units_risked,

            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) = 'pending'
                    THEN stake
                    ELSE 0
                END
            ), 0) AS open_risk
        FROM user_bets
        WHERE user_id = %s
    """, (current_user.id,))

    summary = {
        "total_bets": 0,
        "pending_bets": 0,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "total_profit": 0,
        "open_risk": 0,
        "win_rate": 0
    }

    if not summary_df.empty:
        row = summary_df.iloc[0]

        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        graded = wins + losses

        summary = {
            "total_bets": int(row["total_bets"] or 0),
            "pending_bets": int(row["pending_bets"] or 0),
            "wins": wins,
            "losses": losses,
            "pushes": int(row["pushes"] or 0),
            "total_profit": float(row["total_profit"] or 0),
            "open_risk": float(row["open_risk"] or 0),
            "win_rate": round(
                wins / graded * 100,
                1
            ) if graded else 0
        }


    dashboard_df = read_sql("""
        SELECT
            COALESCE(SUM(CASE
                WHEN settled_at::date = CURRENT_DATE
                THEN profit ELSE 0 END
            ), 0) AS today_profit,

            COALESCE(SUM(CASE
                WHEN settled_at >= DATE_TRUNC('week', CURRENT_DATE)
                THEN profit ELSE 0 END
            ), 0) AS week_profit,

            COALESCE(SUM(CASE
                WHEN settled_at >= DATE_TRUNC('month', CURRENT_DATE)
                THEN profit ELSE 0 END
            ), 0) AS month_profit,

            COALESCE(SUM(CASE
                WHEN LOWER(COALESCE(status, '')) <> 'pending'
                THEN profit ELSE 0 END
            ), 0) AS all_time_profit,

            COALESCE(SUM(CASE
                WHEN LOWER(COALESCE(status, '')) IN ('won', 'lost')
                THEN stake ELSE 0 END
            ), 0) AS settled_stake,

            COALESCE(SUM(CASE
                WHEN LOWER(COALESCE(status, '')) <> 'pending'
                THEN units ELSE 0 END
            ), 0) AS total_units,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, 'pending')) = 'pending'
            ) AS open_bets,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,

            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,

            MAX(CASE
                WHEN LOWER(COALESCE(status, '')) = 'won'
                THEN profit END
            ) AS largest_win,

            MIN(CASE
                WHEN LOWER(COALESCE(status, '')) = 'lost'
                THEN profit END
            ) AS largest_loss

        FROM user_bets
        WHERE user_id = %s
    """, (current_user.id,))

    dashboard_summary = {
        "current_bankroll": 0,
        "today_profit": 0,
        "week_profit": 0,
        "month_profit": 0,
        "all_time_profit": 0,
        "roi": 0,
        "total_units": 0,
        "open_bets": 0,
        "win_rate": 0,
        "largest_win": 0,
        "largest_loss": 0
    }

    if not dashboard_df.empty:
        row = dashboard_df.iloc[0]

        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        graded = wins + losses
        settled_stake = float(row["settled_stake"] or 0)
        all_time_profit = float(row["all_time_profit"] or 0)

        dashboard_summary.update({
            "today_profit": float(row["today_profit"] or 0),
            "week_profit": float(row["week_profit"] or 0),
            "month_profit": float(row["month_profit"] or 0),
            "all_time_profit": all_time_profit,
            "roi": round(
                all_time_profit / settled_stake * 100,
                1
            ) if settled_stake else 0,
            "total_units": float(row["total_units"] or 0),
            "open_bets": int(row["open_bets"] or 0),
            "win_rate": round(
                wins / graded * 100,
                1
            ) if graded else 0,
            "largest_win": float(row["largest_win"] or 0),
            "largest_loss": float(row["largest_loss"] or 0)
        })

    bankrolls_df = read_sql("""
        SELECT
            id,
            name,
            starting_balance,
            current_balance,
            unit_percentage,
            auto_resize,
            is_default
        FROM user_bankrolls
        WHERE user_id = %s
        ORDER BY
            is_default DESC,
            created_at ASC
    """, (current_user.id,))

    bankrolls = (
        bankrolls_df.to_dict("records")
        if not bankrolls_df.empty
        else []
    )


    dashboard_summary["current_bankroll"] = round(
        sum(
            float(bankroll.get("current_balance") or 0)
            for bankroll in bankrolls
        ),
        2
    )


    bankroll_chart_df = read_sql("""
        SELECT
            bt.created_at,
            bt.balance_after,
            ub.name AS bankroll_name
        FROM bankroll_transactions bt
        JOIN user_bankrolls ub
          ON ub.id = bt.bankroll_id
        WHERE bt.user_id = %s
          AND ub.is_default = TRUE
          AND bt.balance_after IS NOT NULL
        ORDER BY bt.created_at ASC
        LIMIT 500
    """, (current_user.id,))

    bankroll_chart = {
        "bankroll_name": (
            next(
                (
                    bankroll.get("name")
                    for bankroll in bankrolls
                    if bankroll.get("is_default")
                ),
                "Main Bankroll"
            )
        ),
        "labels": [],
        "values": []
    }

    if not bankroll_chart_df.empty:
        for _, chart_row in bankroll_chart_df.iterrows():
            created_at = chart_row["created_at"]

            if hasattr(created_at, "strftime"):
                label = created_at.strftime("%b %d")
            else:
                label = str(created_at)

            bankroll_chart["labels"].append(label)
            bankroll_chart["values"].append(
                round(float(chart_row["balance_after"] or 0), 2)
            )

    if not bankroll_chart["values"]:
        default_bankroll = next(
            (
                bankroll
                for bankroll in bankrolls
                if bankroll.get("is_default")
            ),
            bankrolls[0] if bankrolls else None
        )

        if default_bankroll:
            bankroll_chart["labels"] = ["Current"]
            bankroll_chart["values"] = [
                round(
                    float(default_bankroll.get("current_balance") or 0),
                    2
                )
            ]

    streak_df = read_sql("""
        SELECT
            LOWER(COALESCE(result, status)) AS result
        FROM user_bets
        WHERE user_id = %s
          AND LOWER(COALESCE(result, status, '')) IN ('won', 'lost')
        ORDER BY settled_at DESC NULLS LAST, id DESC
    """, (current_user.id,))

    streak_summary = calculate_bet_streaks(
        streak_df["result"].tolist()
        if not streak_df.empty
        else []
    )

    category_df = read_sql("""
        SELECT
            'sport' AS category_type,
            COALESCE(NULLIF(TRIM(sport), ''), 'Unknown') AS category_name,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) IN ('won', 'lost')
            ) AS decisions,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) <> 'pending'
                    THEN profit ELSE 0
                END
            ), 0) AS profit,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) IN ('won', 'lost')
                    THEN stake ELSE 0
                END
            ), 0) AS stake
        FROM user_bets
        WHERE user_id = %s
        GROUP BY 2

        UNION ALL

        SELECT
            'sportsbook' AS category_type,
            COALESCE(NULLIF(TRIM(sportsbook), ''), 'Unknown') AS category_name,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) IN ('won', 'lost')
            ) AS decisions,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) <> 'pending'
                    THEN profit ELSE 0
                END
            ), 0) AS profit,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) IN ('won', 'lost')
                    THEN stake ELSE 0
                END
            ), 0) AS stake
        FROM user_bets
        WHERE user_id = %s
        GROUP BY 2

        UNION ALL

        SELECT
            'bet_type' AS category_type,
            INITCAP(COALESCE(NULLIF(TRIM(bet_type), ''), 'straight')) AS category_name,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) IN ('won', 'lost')
            ) AS decisions,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) <> 'pending'
                    THEN profit ELSE 0
                END
            ), 0) AS profit,
            COALESCE(SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) IN ('won', 'lost')
                    THEN stake ELSE 0
                END
            ), 0) AS stake
        FROM user_bets
        WHERE user_id = %s
        GROUP BY 2
    """, (
        current_user.id,
        current_user.id,
        current_user.id
    ))

    performance_insights = {
        "best_sport": None,
        "best_sportsbook": None,
        "best_bet_type": None,
        "best_prop": None
    }

    if not category_df.empty:
        category_df["decisions"] = pd.to_numeric(
            category_df["decisions"],
            errors="coerce"
        ).fillna(0)

        category_df["wins"] = pd.to_numeric(
            category_df["wins"],
            errors="coerce"
        ).fillna(0)

        category_df["losses"] = pd.to_numeric(
            category_df["losses"],
            errors="coerce"
        ).fillna(0)

        category_df["profit"] = pd.to_numeric(
            category_df["profit"],
            errors="coerce"
        ).fillna(0)

        category_df["stake"] = pd.to_numeric(
            category_df["stake"],
            errors="coerce"
        ).fillna(0)

        category_df["roi"] = category_df.apply(
            lambda row: (
                float(row["profit"]) / float(row["stake"]) * 100
                if float(row["stake"]) > 0
                else 0
            ),
            axis=1
        )

        category_df["win_rate"] = category_df.apply(
            lambda row: (
                float(row["wins"]) / float(row["decisions"]) * 100
                if float(row["decisions"]) > 0
                else 0
            ),
            axis=1
        )

        def best_category(category_type):
            eligible = category_df[
                (category_df["category_type"] == category_type)
                & (category_df["decisions"] >= 3)
            ].copy()

            if eligible.empty:
                eligible = category_df[
                    category_df["category_type"] == category_type
                ].copy()

            if eligible.empty:
                return None

            eligible = eligible.sort_values(
                ["profit", "roi", "decisions"],
                ascending=[False, False, False]
            )

            row = eligible.iloc[0]

            return {
                "name": str(row["category_name"]),
                "profit": round(float(row["profit"]), 2),
                "roi": round(float(row["roi"]), 1),
                "win_rate": round(float(row["win_rate"]), 1),
                "record": (
                    f"{int(row['wins'])}-{int(row['losses'])}"
                ),
                "sample": int(row["decisions"])
            }

        performance_insights["best_sport"] = best_category("sport")
        performance_insights["best_sportsbook"] = best_category("sportsbook")
        performance_insights["best_bet_type"] = best_category("bet_type")

    prop_df = read_sql("""
        SELECT
            COALESCE(NULLIF(TRIM(ubl.prop), ''), 'Other') AS prop,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(ubl.status, '')) IN ('won', 'lost')
            ) AS decisions,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(ubl.status, '')) = 'won'
            ) AS wins,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(ubl.status, '')) = 'lost'
            ) AS losses
        FROM user_bet_legs ubl
        JOIN user_bets ub
          ON ub.id = ubl.user_bet_id
        WHERE ub.user_id = %s
        GROUP BY 1
        ORDER BY 2 DESC
    """, (current_user.id,))

    if not prop_df.empty:
        prop_df["decisions"] = pd.to_numeric(
            prop_df["decisions"],
            errors="coerce"
        ).fillna(0)

        prop_df["wins"] = pd.to_numeric(
            prop_df["wins"],
            errors="coerce"
        ).fillna(0)

        prop_df["losses"] = pd.to_numeric(
            prop_df["losses"],
            errors="coerce"
        ).fillna(0)

        prop_df["win_rate"] = prop_df.apply(
            lambda row: (
                float(row["wins"]) / float(row["decisions"]) * 100
                if float(row["decisions"]) > 0
                else 0
            ),
            axis=1
        )

        eligible_props = prop_df[
            prop_df["decisions"] >= 3
        ].copy()

        if eligible_props.empty:
            eligible_props = prop_df[
                prop_df["decisions"] > 0
            ].copy()

        if not eligible_props.empty:
            eligible_props = eligible_props.sort_values(
                ["win_rate", "decisions"],
                ascending=[False, False]
            )

            prop_row = eligible_props.iloc[0]

            performance_insights["best_prop"] = {
                "name": str(prop_row["prop"]),
                "win_rate": round(
                    float(prop_row["win_rate"]),
                    1
                ),
                "record": (
                    f"{int(prop_row['wins'])}-"
                    f"{int(prop_row['losses'])}"
                ),
                "sample": int(prop_row["decisions"])
            }

    calendar_df = read_sql("""
        SELECT
            settled_at::date AS activity_date,
            COALESCE(SUM(profit), 0) AS profit,
            COUNT(*) AS ticket_count,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'won'
            ) AS wins,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) = 'lost'
            ) AS losses,
            COUNT(*) FILTER (
                WHERE LOWER(COALESCE(status, '')) IN ('push', 'void')
            ) AS pushes
        FROM user_bets
        WHERE user_id = %s
          AND settled_at IS NOT NULL
          AND LOWER(COALESCE(status, 'pending')) <> 'pending'
        GROUP BY settled_at::date
        ORDER BY activity_date
    """, (current_user.id,))

    calendar_daily = {}

    if not calendar_df.empty:
        for _, day_row in calendar_df.iterrows():
            activity_date = day_row["activity_date"]

            if hasattr(activity_date, "strftime"):
                date_key = activity_date.strftime("%Y-%m-%d")
            else:
                date_key = str(activity_date)

            calendar_daily[date_key] = {
                "profit": round(float(day_row["profit"] or 0), 2),
                "ticket_count": int(day_row["ticket_count"] or 0),
                "wins": int(day_row["wins"] or 0),
                "losses": int(day_row["losses"] or 0),
                "pushes": int(day_row["pushes"] or 0)
            }

    calendar_bets_df = read_sql("""
        SELECT
            id,
            settled_at::date AS activity_date,
            COALESCE(NULLIF(TRIM(title), ''), 'Tracked Bet') AS title,
            sport,
            bet_type,
            sportsbook,
            status,
            profit
        FROM user_bets
        WHERE user_id = %s
          AND settled_at IS NOT NULL
          AND LOWER(COALESCE(status, 'pending')) <> 'pending'
        ORDER BY settled_at DESC, id DESC
        LIMIT 1000
    """, (current_user.id,))

    calendar_bets = {}

    if not calendar_bets_df.empty:
        for _, calendar_row in calendar_bets_df.iterrows():
            activity_date = calendar_row["activity_date"]

            if hasattr(activity_date, "strftime"):
                date_key = activity_date.strftime("%Y-%m-%d")
            else:
                date_key = str(activity_date)

            calendar_bets.setdefault(date_key, []).append({
                "id": int(calendar_row["id"]),
                "title": str(calendar_row["title"]),
                "sport": str(calendar_row["sport"] or ""),
                "bet_type": str(calendar_row["bet_type"] or "straight"),
                "sportsbook": str(calendar_row["sportsbook"] or ""),
                "status": str(calendar_row["status"] or ""),
                "profit": (
                    round(float(calendar_row["profit"]), 2)
                    if calendar_row["profit"] is not None
                    else None
                )
            })

    sports_df = read_sql("""
        SELECT DISTINCT sport
        FROM user_bets
        WHERE user_id = %s
          AND sport IS NOT NULL
          AND TRIM(sport) <> ''
        ORDER BY sport
    """, (current_user.id,))

    sports = (
        sports_df["sport"].dropna().astype(str).tolist()
        if not sports_df.empty
        else []
    )
    
    now_hour = datetime.now(
        ZoneInfo("America/Toronto")
    ).hour

    return render_template(
        "my_bets.html",
        active_page="my_hub",
        bets=bets,
        summary=summary,
        dashboard_summary=dashboard_summary,
        bankroll_chart=bankroll_chart,
        streak_summary=streak_summary,
        performance_insights=performance_insights,
        calendar_daily=calendar_daily,
        calendar_bets=calendar_bets,
        bankrolls=bankrolls,
        sports=sports,
        status_filter=status_filter,
        bet_type_filter=bet_type_filter,
        sport_filter=sport_filter,
        now_hour=now_hour
    )
@app.route("/my-hub/bets/<int:bet_id>/edit-data", methods=["GET"])
@login_required
def personal_bet_edit_data(bet_id):
    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ub.id,
                    ub.user_id,
                    ub.bankroll_id,
                    ub.title,
                    ub.sport,
                    ub.league,
                    ub.source,
                    ub.sportsbook,
                    ub.stake,
                    ub.notes,
                    ub.status,
                    ub.bet_type
                FROM user_bets ub
                WHERE ub.id = %s
                  AND ub.user_id = %s
            """, (bet_id, current_user.id))

            bet = cur.fetchone()

            if not bet:
                return jsonify({
                    "success": False,
                    "error": "Bet not found."
                }), 404

            if str(bet[10] or "pending").lower() != "pending":
                return jsonify({
                    "success": False,
                    "error": "Only pending bets can be edited."
                }), 400

            cur.execute("""
                SELECT
                    id,
                    selection_type,
                    selection_name,
                    player_name,
                    team_name,
                    prop,
                    ou,
                    COALESCE(user_line, line) AS display_line,
                    COALESCE(user_odds, odds) AS display_odds,
                    start_time,
                    sort_order
                FROM user_bet_legs
                WHERE user_bet_id = %s
                ORDER BY COALESCE(sort_order, id)
            """, (bet_id,))

            leg_rows = cur.fetchall()

        now_value = datetime.now(
            ZoneInfo("America/Toronto")
        ).replace(tzinfo=None)

        event_starts = []

        for row in leg_rows:
            start_time = row[9]

            if start_time is not None:
                if getattr(start_time, "tzinfo", None):
                    start_time = start_time.replace(tzinfo=None)

                event_starts.append(start_time)

        earliest_start = min(event_starts) if event_starts else None

        if earliest_start is not None and now_value >= earliest_start:
            return jsonify({
                "success": False,
                "error": "This ticket is locked because an event has started."
            }), 400

        legs = []

        for row in leg_rows:
            start_time = row[9]

            if start_time is not None:
                if getattr(start_time, "tzinfo", None):
                    start_time = start_time.replace(tzinfo=None)

                start_value = start_time.strftime("%Y-%m-%dT%H:%M")
            else:
                start_value = ""

            legs.append({
                "id": row[0],
                "selection_type": row[1] or "",
                "selection_name": (
                    row[2]
                    or row[3]
                    or row[4]
                    or ""
                ),
                "prop": row[5] or "",
                "ou": row[6] or "",
                "line": (
                    float(row[7])
                    if row[7] is not None
                    else ""
                ),
                "odds": row[8],
                "event_start": start_value,
                "sort_order": row[10]
            })

        return jsonify({
            "success": True,
            "bet": {
                "id": bet[0],
                "bankroll_id": bet[2],
                "title": bet[3] or "",
                "sport": bet[4] or "",
                "league": bet[5] or "",
                "source": bet[6] or "",
                "sportsbook": bet[7] or "",
                "stake": (
                    float(bet[8])
                    if bet[8] is not None
                    else ""
                ),
                "notes": bet[9] or "",
                "bet_type": bet[11] or "straight",
                "legs": legs
            }
        })

    finally:
        conn.close()


@app.route("/my-hub/bets/<int:bet_id>/edit", methods=["POST"])
@login_required
def edit_personal_bet(bet_id):
    bankroll_id = request.form.get("bankroll_id", type=int)
    sport = clean_text(request.form.get("sport"))
    league = clean_text(request.form.get("league"))
    source = clean_text(request.form.get("source"))
    sportsbook = clean_text(request.form.get("sportsbook"))
    title = clean_text(request.form.get("title"))
    notes = clean_text(request.form.get("notes"))

    selection_types = request.form.getlist("selection_type[]")
    selection_names = request.form.getlist("selection_name[]")
    props = request.form.getlist("prop[]")
    sides = request.form.getlist("ou[]")
    lines_raw = request.form.getlist("line[]")
    odds_raw = request.form.getlist("leg_odds[]")
    event_starts = request.form.getlist("event_start[]")

    try:
        stake = float(request.form.get("stake"))
    except (TypeError, ValueError):
        flash("Enter a valid stake.", "error")
        return redirect(url_for("my_bets_page"))

    leg_count = len(selection_names)

    if not bankroll_id or not sport or not sportsbook:
        flash("Complete the required ticket fields.", "error")
        return redirect(url_for("my_bets_page"))

    if stake <= 0:
        flash("Stake must be greater than zero.", "error")
        return redirect(url_for("my_bets_page"))

    if leg_count < 1 or leg_count > 20:
        flash("Tickets must contain between 1 and 20 legs.", "error")
        return redirect(url_for("my_bets_page"))

    submitted_lists = [
        selection_types,
        props,
        sides,
        lines_raw,
        odds_raw,
        event_starts
    ]

    if any(len(items) != leg_count for items in submitted_lists):
        flash("The ticket legs were incomplete.", "error")
        return redirect(url_for("my_bets_page"))

    legs = []
    combined_decimal = 1.0

    for index in range(leg_count):
        selection_type = clean_text(selection_types[index])
        selection_name = clean_text(selection_names[index])
        prop = clean_text(props[index])
        ou = clean_text(sides[index]).lower()
        event_start = clean_text(event_starts[index])

        if not selection_type or not selection_name or not event_start:
            flash(
                f"Complete all required fields for leg {index + 1}.",
                "error"
            )
            return redirect(url_for("my_bets_page"))

        try:
            odds = int(odds_raw[index])
        except (TypeError, ValueError):
            flash(
                f"Enter valid odds for leg {index + 1}.",
                "error"
            )
            return redirect(url_for("my_bets_page"))

        if odds == 0:
            flash(
                f"Odds cannot be zero for leg {index + 1}.",
                "error"
            )
            return redirect(url_for("my_bets_page"))

        line = None
        line_text = clean_text(lines_raw[index])

        if line_text:
            try:
                line = float(line_text)
            except ValueError:
                flash(
                    f"Enter a valid line for leg {index + 1}.",
                    "error"
                )
                return redirect(url_for("my_bets_page"))

        decimal_odds = (
            1 + odds / 100
            if odds > 0
            else 1 + 100 / abs(odds)
        )

        combined_decimal *= decimal_odds

        legs.append({
            "selection_type": selection_type,
            "selection_name": selection_name,
            "prop": prop,
            "ou": ou,
            "line": line,
            "odds": odds,
            "event_start": event_start
        })

    if not title or title.lower() == "nan":
        title = generate_ticket_title(legs)

    combined_odds = (
        round((combined_decimal - 1) * 100)
        if combined_decimal >= 2
        else round(-100 / (combined_decimal - 1))
    )

    potential_profit = round(
        stake * (combined_decimal - 1),
        2
    )
    potential_return = round(
        stake + potential_profit,
        2
    )

    bet_type = (
        "straight"
        if leg_count == 1
        else "parlay"
    )

    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ub.ticket_id,
                        ub.status,
                        MIN(ubl.start_time) AS earliest_start
                    FROM user_bets ub
                    LEFT JOIN user_bet_legs ubl
                        ON ubl.user_bet_id = ub.id
                    WHERE ub.id = %s
                      AND ub.user_id = %s
                    GROUP BY
                        ub.id,
                        ub.ticket_id,
                        ub.status
                    FOR UPDATE OF ub
                """, (
                    bet_id,
                    current_user.id
                ))

                existing = cur.fetchone()

                if not existing:
                    flash("Bet not found.", "error")
                    return redirect(url_for("my_bets_page"))

                ticket_id = existing[0]
                current_status = str(
                    existing[1] or "pending"
                ).lower()
                earliest_start = existing[2]

                if current_status != "pending":
                    flash(
                        "Only pending bets can be edited.",
                        "error"
                    )
                    return redirect(url_for("my_bets_page"))

                now_value = datetime.now(
                    ZoneInfo("America/Toronto")
                ).replace(tzinfo=None)

                if earliest_start is not None:
                    if getattr(
                        earliest_start,
                        "tzinfo",
                        None
                    ):
                        earliest_start = (
                            earliest_start.replace(
                                tzinfo=None
                            )
                        )

                    if now_value >= earliest_start:
                        flash(
                            "This ticket is locked because "
                            "an event has started.",
                            "error"
                        )
                        return redirect(
                            url_for("my_bets_page")
                        )

                cur.execute("""
                    SELECT
                        current_balance,
                        unit_percentage
                    FROM user_bankrolls
                    WHERE id = %s
                      AND user_id = %s
                    FOR UPDATE
                """, (
                    bankroll_id,
                    current_user.id
                ))

                bankroll = cur.fetchone()

                if not bankroll:
                    flash("Bankroll not found.", "error")
                    return redirect(url_for("my_bets_page"))

                current_balance = float(
                    bankroll[0] or 0
                )
                unit_percentage = float(
                    bankroll[1] or 0.01
                )
                unit_value = (
                    current_balance
                    * unit_percentage
                )
                units = (
                    stake / unit_value
                    if unit_value > 0
                    else None
                )

                straight_line = (
                    legs[0]["line"]
                    if leg_count == 1
                    else None
                )

                cur.execute("""
                    UPDATE user_bets
                    SET
                        bankroll_id = %s,
                        sport = %s,
                        league = %s,
                        source = %s,
                        sportsbook = %s,
                        odds_taken = %s,
                        line_taken = %s,
                        stake = %s,
                        units = %s,
                        unit_value = %s,
                        bet_type = %s,
                        user_combined_odds = %s,
                        combined_odds = %s,
                        potential_profit = %s,
                        potential_return = %s,
                        bankroll_balance_at_bet = %s,
                        unit_percentage = %s,
                        title = %s,
                        notes = %s
                    WHERE id = %s
                      AND user_id = %s
                """, (
                    bankroll_id,
                    sport,
                    league or None,
                    source or None,
                    sportsbook,
                    combined_odds,
                    straight_line,
                    stake,
                    units,
                    unit_value,
                    bet_type,
                    combined_odds,
                    combined_odds,
                    potential_profit,
                    potential_return,
                    current_balance,
                    unit_percentage,
                    title or None,
                    notes or None,
                    bet_id,
                    current_user.id
                ))

                cur.execute("""
                    DELETE FROM user_bet_legs
                    WHERE user_bet_id = %s
                """, (bet_id,))

                for sort_order, leg in enumerate(
                    legs,
                    start=1
                ):
                    cur.execute("""
                        INSERT INTO user_bet_legs (
                            ticket_id,
                            user_bet_id,
                            player_name,
                            prop,
                            ou,
                            line,
                            odds,
                            sportsbook,
                            start_time,
                            status,
                            created_at,
                            official_sportsbook,
                            official_odds,
                            user_sportsbook,
                            user_odds,
                            official_line,
                            user_line,
                            sport,
                            league,
                            selection_type,
                            selection_name,
                            team_name,
                            opponent,
                            result,
                            sort_order
                        )
                        VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s::timestamp,
                            'pending',
                            NOW(),
                            NULL, NULL,
                            %s, %s,
                            NULL, %s,
                            %s, %s,
                            %s, %s,
                            NULL, NULL, NULL,
                            %s
                        )
                    """, (
                        str(ticket_id),
                        bet_id,
                        (
                            leg["selection_name"]
                            if leg["selection_type"]
                            == "Player Prop"
                            else None
                        ),
                        (
                            leg["prop"]
                            or leg["selection_type"]
                        ),
                        leg["ou"] or None,
                        leg["line"],
                        leg["odds"],
                        sportsbook,
                        leg["event_start"],
                        sportsbook,
                        leg["odds"],
                        leg["line"],
                        sport,
                        league or None,
                        leg["selection_type"],
                        leg["selection_name"],
                        sort_order
                    ))

        flash("Ticket updated.", "success")

    except Exception as exc:
        print("Parlay edit error:", exc)
        flash(
            "The ticket could not be updated.",
            "error"
        )

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))

@app.route("/my-hub/bets/<int:bet_id>/delete", methods=["POST"])
@login_required
def delete_personal_bet(bet_id):

    conn = get_conn()
    cur = conn.cursor()

    try:

        cur.execute("""
            SELECT
                ub.user_id,
                MIN(ubl.start_time)
            FROM user_bets ub
            LEFT JOIN user_bet_legs ubl
                ON ub.id = ubl.user_bet_id
            WHERE ub.id=%s
            GROUP BY ub.user_id
        """, (bet_id,))

        row = cur.fetchone()

        if not row:
            flash("Bet not found.", "error")
            return redirect(url_for("my_bets_page"))

        owner_id, event_start = row

        if owner_id != current_user.id:
            flash("Unauthorized.", "error")
            return redirect(url_for("my_bets_page"))

        now = datetime.now(
            ZoneInfo("America/Toronto")
        ).replace(tzinfo=None)

        if event_start:

            if getattr(event_start, "tzinfo", None):
                event_start = event_start.replace(tzinfo=None)

            if now >= event_start:
                flash(
                    "This bet can no longer be deleted.",
                    "error"
                )
                return redirect(url_for("my_bets_page"))

        cur.execute("""
            INSERT INTO deleted_bets_log (
                user_id,
                bet_id,
                bet_snapshot,
                legs_snapshot,
                deleted_at
            )
            SELECT
                %s,
                ub.id,
                TO_JSONB(ub),
                COALESCE(
                    (
                        SELECT JSONB_AGG(TO_JSONB(ubl))
                        FROM user_bet_legs ubl
                        WHERE ubl.user_bet_id = ub.id
                    ),
                    '[]'::jsonb
                ),
                NOW()
            FROM user_bets ub
            WHERE ub.id = %s
            AND ub.user_id = %s
        """, (
            current_user.id,
            bet_id,
            current_user.id
        ))

        cur.execute(
            "DELETE FROM user_bet_legs WHERE user_bet_id=%s",
            (bet_id,)
        )

        cur.execute(
            "DELETE FROM user_bets WHERE id=%s",
            (bet_id,)
        )

        conn.commit()

        flash("Bet deleted.", "success")

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))



# ================= VERIFIED PROVIDER BETS =================

PROVIDER_MARKET_LABELS = {
    "batter_hits": "Hits",
    "batter_home_runs": "Home Runs",
    "batter_total_bases": "Total Bases",
    "batter_rbis": "RBI",
    "batter_runs": "Runs",
    "pitcher_strikeouts": "Pitcher Strikeouts",
    "pitcher_outs": "Pitcher Outs",
}


def provider_market_label(market_key):
    key = clean_text(market_key)
    return PROVIDER_MARKET_LABELS.get(
        key,
        key.replace("_", " ").title()
    )


PROVIDER_PLAYER_SUFFIXES = (
    "strikeouts thrown",
    "pitching strikeouts",
    "pitcher strikeouts",
    "outs recorded",
    "pitcher outs",
    "hits allowed",
    "walks allowed",
    "earned runs allowed",
    "runs allowed",
    "home runs allowed",
    "total bases",
    "home runs",
    "runs batted in",
    "rbis",
    "hits",
    "runs",
)


def normalize_provider_player_name(value):
    """Collapse provider aliases into one clean display player name."""
    name = clean_text(value)
    if not name:
        return ""

    # Team suffixes such as "Dylan Cease (SD)" or "Dylan Cease (TOR)".
    name = re.sub(r"\s*\([A-Z0-9]{2,4}\)\s*$", "", name).strip()

    # Provider variants such as "Dylan Cease Strikeouts Thrown".
    lowered = name.lower()
    for suffix in PROVIDER_PLAYER_SUFFIXES:
        marker = " " + suffix
        if lowered.endswith(marker):
            name = name[: -len(marker)].strip()
            lowered = name.lower()
            break

    return re.sub(r"\s+", " ", name).strip()


def provider_value_or_none(value):
    """Convert pandas NaN/NaT values into JSON-safe None."""
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    return value


def provider_int_or_none(value):
    value = provider_value_or_none(value)
    return int(value) if value is not None else None


def provider_float_or_none(value):
    value = provider_value_or_none(value)
    return float(value) if value is not None else None


def provider_sport_display(sport_key):
    mapping = {
        "baseball_mlb": ("MLB", "MLB"),
        "basketball_nba": ("NBA", "NBA"),
        "basketball_wnba": ("WNBA", "WNBA"),
        "americanfootball_nfl": ("NFL", "NFL"),
        "icehockey_nhl": ("NHL", "NHL"),
    }
    return mapping.get(sport_key, (sport_key.upper(), sport_key.upper()))



@app.route("/api/provider/games")
@login_required
def provider_games_api():
    sport_key = clean_text(
        request.args.get("sport", "baseball_mlb")
    ) or "baseball_mlb"

    rows = read_sql("""
        SELECT
            pe.provider_event_id,
            pe.sport_key,
            pe.home_team,
            pe.away_team,
            pe.commence_time,
            pe.status,
            pe.live,
            COUNT(pmc.id) AS cached_selections,
            COUNT(
                DISTINCT COALESCE(
                    pmc.canonical_player_id::text,
                    'name:' || LOWER(pmc.clean_player_name)
                )
            ) AS player_count
        FROM provider_events pe
        JOIN provider_market_cache pmc
          ON pmc.provider = pe.provider
         AND pmc.provider_event_id = pe.provider_event_id
        WHERE pe.provider = 'prop_line'
          AND pe.sport_key = %s
          AND pe.commence_time > NOW()
          AND pe.commence_time <= NOW() + INTERVAL '4 days'
          AND COALESCE(pe.live, FALSE) = FALSE
          AND LOWER(COALESCE(pe.status, 'upcoming')) NOT IN (
              'final', 'completed', 'complete', 'cancelled', 'canceled'
          )
        GROUP BY
            pe.provider_event_id,
            pe.sport_key,
            pe.home_team,
            pe.away_team,
            pe.commence_time,
            pe.status,
            pe.live
        ORDER BY pe.commence_time ASC
    """, (sport_key,))

    games = []

    for row in rows.to_dict("records") if not rows.empty else []:
        start = row.get("commence_time")

        games.append({
            "event_id": str(row.get("provider_event_id")),
            "sport_key": row.get("sport_key"),
            "home_team": row.get("home_team"),
            "away_team": row.get("away_team"),
            "commence_time": start.isoformat() if start else None,
            "player_count": int(
                provider_value_or_none(row.get("player_count")) or 0
            ),
            "cached_selections": int(
                provider_value_or_none(row.get("cached_selections")) or 0
            ),
            "label": (
                f"{row.get('away_team')} @ {row.get('home_team')}"
            ),
        })

    return jsonify({"games": games})


@app.route("/api/provider/players")
@login_required
def provider_players_api():
    event_id = clean_text(request.args.get("event_id"))

    if not event_id:
        return jsonify({"players": []})

    rows = read_sql("""
        SELECT
            canonical_player_id,
            clean_player_name AS name,
            COUNT(*) AS selection_count,
            COUNT(DISTINCT market_key) AS market_count
        FROM provider_market_cache
        WHERE provider = 'prop_line'
          AND provider_event_id = %s
          AND clean_player_name <> ''
        GROUP BY
            canonical_player_id,
            clean_player_name
        ORDER BY clean_player_name
    """, (event_id,))

    players = []

    for row in rows.to_dict("records") if not rows.empty else []:
        players.append({
            "canonical_player_id": provider_int_or_none(
                row.get("canonical_player_id")
            ),
            "name": clean_text(row.get("name")),
            "selection_count": int(
                provider_value_or_none(
                    row.get("selection_count")
                ) or 0
            ),
            "market_count": int(
                provider_value_or_none(
                    row.get("market_count")
                ) or 0
            ),
        })

    return jsonify({"players": players})


@app.route("/api/provider/selections")
@login_required
def provider_selections_api():
    event_id = clean_text(request.args.get("event_id"))
    requested_player = clean_text(request.args.get("player"))
    canonical_player_id = request.args.get(
        "canonical_player_id",
        type=int,
    )

    if not event_id or (
        not canonical_player_id and not requested_player
    ):
        return jsonify({
            "player": requested_player,
            "selections": [],
            "markets": [],
        })

    rows = read_sql("""
        SELECT
            summary_id,
            market_key,
            market_label,
            period,
            clean_player_name,
            raw_player_name,
            outcome_name,
            line,
            books_available,
            best_odds,
            best_bookmaker_title,
            worst_odds,
            average_odds,
            is_main
        FROM provider_market_cache
        WHERE provider = 'prop_line'
          AND provider_event_id = %s
          AND (
            canonical_player_id = %s
        OR (
            canonical_player_id IS NULL
            AND LOWER(clean_player_name) = LOWER(%s)
        )
    )
        ORDER BY
            market_label,
            is_main DESC,
            outcome_name,
            line
    """, (
        event_id,
        canonical_player_id,
        requested_player,
    ))

    selections = []

    for row in rows.to_dict("records") if not rows.empty else []:
        summary_id = provider_int_or_none(row.get("summary_id"))

        # A provider summary id is required to save a verified bet.
        if summary_id is None:
            continue

        line = provider_float_or_none(row.get("line"))

        item = {
            "summary_id": summary_id,
            "market_key": clean_text(row.get("market_key")),
            "market_label": clean_text(row.get("market_label")),
            "outcome_name": clean_text(row.get("outcome_name")),
            "line": line,
            "books_available": int(
                provider_value_or_none(
                    row.get("books_available")
                ) or 0
            ),
            "best_odds": provider_int_or_none(
                row.get("best_odds")
            ),
            "best_bookmaker_title":
                provider_value_or_none(
                    row.get("best_bookmaker_title")
                ),
            "worst_odds": provider_int_or_none(
                row.get("worst_odds")
            ),
            "average_odds": provider_int_or_none(
                row.get("average_odds")
            ),
            "raw_player_name": clean_text(
                row.get("raw_player_name")
            ),
            "is_main": bool(row.get("is_main")),
        }

        line_text = (
            ""
            if line is None
            else f" {line:g}"
        )

        item["label"] = (
            f"{clean_text(item['outcome_name']).title()}"
            f"{line_text}"
        )

        selections.append(item)

    market_groups = {}

    for item in selections:
        market = market_groups.setdefault(
            item["market_key"],
            {
                "market_key": item["market_key"],
                "market_label": item["market_label"],
                "main": [],
                "alternates": [],
            },
        )

        market[
            "main" if item["is_main"] else "alternates"
        ].append(item)

    markets = sorted(
        market_groups.values(),
        key=lambda item: item["market_label"].lower(),
    )

    for market in markets:
        market["main"].sort(
            key=lambda item: (
                item["outcome_name"].lower(),
                item["line"] is None,
                item["line"] or 0,
            )
        )

        market["alternates"].sort(
            key=lambda item: (
                item["outcome_name"].lower(),
                item["line"] is None,
                item["line"] or 0,
            )
        )

    return jsonify({
        "player": requested_player,
        "selections": selections,
        "markets": markets,
    })


@app.route("/api/provider/books")
@login_required
def provider_books_api():
    summary_id = request.args.get(
        "summary_id",
        type=int,
    )

    if not summary_id:
        return jsonify({
            "books": [],
            "summary": None,
        })

    rows = read_sql("""
        SELECT
            summary_id,
            books_available,
            best_odds,
            best_bookmaker_key,
            best_bookmaker_title,
            worst_odds,
            average_odds,
            books
        FROM provider_market_cache
        WHERE provider = 'prop_line'
          AND summary_id = %s
        LIMIT 1
    """, (summary_id,))

    if rows.empty:
        return jsonify({
            "books": [],
            "summary": None,
        }), 404

    row = rows.iloc[0].to_dict()
    books = row.get("books") or []

    if not isinstance(books, list):
        books = []

    books = sorted(
        books,
        key=lambda book: (
            int(book.get("display_order") or 100),
            clean_text(
                book.get("bookmaker_title")
                or book.get("bookmaker_key")
            ).lower(),
        ),
    )

    return jsonify({
        "summary": {
            "summary_id": int(row["summary_id"]),
            "books_available": int(
                provider_value_or_none(
                    row.get("books_available")
                ) or len(books)
            ),
            "best_odds": provider_int_or_none(
                row.get("best_odds")
            ),
            "best_bookmaker_key":
                provider_value_or_none(
                    row.get("best_bookmaker_key")
                ),
            "best_bookmaker_title":
                provider_value_or_none(
                    row.get("best_bookmaker_title")
                ),
            "worst_odds": provider_int_or_none(
                row.get("worst_odds")
            ),
            "average_odds": provider_int_or_none(
                row.get("average_odds")
            ),
        },
        "books": books,
    })

@app.route("/my-hub/bets/add-verified", methods=["POST"])
@login_required
def add_verified_bet():
    bankroll_id = request.form.get("bankroll_id", type=int)
    notes = clean_text(request.form.get("notes"))

    try:
        stake = float(request.form.get("stake"))
    except (TypeError, ValueError):
        flash("Enter a valid stake.", "error")
        return redirect(url_for("my_bets_page"))

    try:
        submitted_legs = json.loads(
            clean_text(request.form.get("verified_legs_json")) or "[]"
        )
    except Exception:
        submitted_legs = []

    if not bankroll_id:
        flash("Choose a bankroll.", "error")
        return redirect(url_for("my_bets_page"))

    if stake <= 0:
        flash("Stake must be greater than zero.", "error")
        return redirect(url_for("my_bets_page"))

    if not isinstance(submitted_legs, list) or not (1 <= len(submitted_legs) <= 20):
        flash("Add between 1 and 20 verified legs.", "error")
        return redirect(url_for("my_bets_page"))

    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, current_balance, unit_percentage
                    FROM user_bankrolls
                    WHERE id = %s AND user_id = %s
                    FOR UPDATE
                """, (bankroll_id, current_user.id))

                bankroll = cur.fetchone()
                if not bankroll:
                    raise ValueError("That bankroll could not be found.")

                current_balance = float(bankroll[1] or 0)
                unit_percentage = float(bankroll[2] or 0.01)
                unit_value = current_balance * unit_percentage
                units = stake / unit_value if unit_value > 0 else None

                legs = []
                combined_decimal = 1.0

                for index, submitted in enumerate(submitted_legs, start=1):
                    summary_id = provider_int_or_none(submitted.get("summary_id"))
                    provider_market_id = provider_int_or_none(
                        submitted.get("provider_market_id")
                    )
                    selection_source = clean_text(
                        submitted.get("selection_source")
                    ).lower()

                    if not summary_id or selection_source not in {"book", "average"}:
                        raise ValueError(f"Leg {index} is incomplete.")

                    cur.execute("""
                        SELECT
                            pmc.provider_event_id,
                            pmc.sport_key,
                            pmc.market_key,
                            pmc.market_label,
                            pmc.clean_player_name,
                            pmc.outcome_name,
                            pmc.line,
                            pmc.average_odds,
                            pmc.books,
                            pmc.home_team,
                            pmc.away_team,
                            pmc.commence_time,
                            pe.live,
                            pe.status
                        FROM provider_market_cache pmc
                        JOIN provider_events pe
                          ON pe.provider = pmc.provider
                         AND pe.provider_event_id = pmc.provider_event_id
                        WHERE pmc.provider = 'prop_line'
                          AND pmc.summary_id = %s
                        LIMIT 1
                    """, (summary_id,))

                    cached = cur.fetchone()
                    if not cached:
                        raise ValueError(f"Leg {index} is no longer available.")

                    (
                        provider_event_id,
                        sport_key,
                        market_key,
                        market_label,
                        player_name,
                        outcome_name,
                        line,
                        average_odds,
                        books,
                        home_team,
                        away_team,
                        event_start,
                        live,
                        event_status,
                    ) = cached

                    now_utc = datetime.now(ZoneInfo("UTC"))
                    if event_start and event_start.tzinfo is None:
                        event_start = event_start.replace(tzinfo=ZoneInfo("UTC"))

                    if (
                        live
                        or not event_start
                        or event_start <= now_utc
                        or clean_text(event_status).lower() in {
                            "final", "complete", "completed", "cancelled", "canceled"
                        }
                    ):
                        raise ValueError(f"Leg {index} is locked because the event started.")

                    selected_market_id = None

                    if selection_source == "average":
                        odds = provider_int_or_none(average_odds)
                        if odds is None:
                            raise ValueError(
                                f"Average Market is unavailable for leg {index}."
                            )
                        sportsbook = "Average Market"
                        verification_type = "market"
                    else:
                        cache_books = books if isinstance(books, list) else []
                        selected_book = next(
                            (
                                book for book in cache_books
                                if provider_int_or_none(
                                    book.get("provider_market_id")
                                ) == provider_market_id
                            ),
                            None,
                        )

                        if not selected_book:
                            raise ValueError(
                                f"The sportsbook price for leg {index} is no longer available."
                            )

                        odds = provider_int_or_none(selected_book.get("odds"))
                        if odds is None or odds == 0:
                            raise ValueError(f"Invalid odds for leg {index}.")

                        sportsbook = clean_text(
                            selected_book.get("bookmaker_title")
                            or selected_book.get("bookmaker_key")
                        )
                        selected_market_id = provider_market_id
                        verification_type = "verified"

                    decimal_odds = (
                        1 + odds / 100
                        if odds > 0
                        else 1 + 100 / abs(odds)
                    )
                    combined_decimal *= decimal_odds

                    sport, league = provider_sport_display(sport_key)
                    line_float = float(line) if line is not None else None
                    side = clean_text(outcome_name).lower()
                    line_text = (
                        f" {line_float:g}" if line_float is not None else ""
                    )

                    legs.append({
                        "summary_id": summary_id,
                        "provider_market_id": selected_market_id,
                        "provider_event_id": str(provider_event_id),
                        "market_key": market_key,
                        "market_label": market_label,
                        "player_name": player_name,
                        "side": side,
                        "line": line_float,
                        "odds": odds,
                        "sportsbook": sportsbook,
                        "event_start": event_start,
                        "sport": sport,
                        "league": league,
                        "opponent": f"{away_team} @ {home_team}",
                        "verification_type": verification_type,
                        "display": (
                            f"{player_name} {side.title()}"
                            f"{line_text} {market_label}"
                        ),
                    })

                combined_odds = (
                    int(round((combined_decimal - 1) * 100))
                    if combined_decimal >= 2
                    else int(round(-100 / (combined_decimal - 1)))
                )

                potential_profit = round(stake * (combined_decimal - 1), 2)
                potential_return = round(stake + potential_profit, 2)
                ticket_id = uuid.uuid4()
                leg_count = len(legs)

                books = list(dict.fromkeys(leg["sportsbook"] for leg in legs))
                ticket_sportsbook = books[0] if len(books) == 1 else "Mixed Verified"

                sports = list(dict.fromkeys(leg["sport"] for leg in legs))
                ticket_sport = sports[0] if len(sports) == 1 else "MULTI"

                leagues = list(dict.fromkeys(leg["league"] for leg in legs))
                ticket_league = leagues[0] if len(leagues) == 1 else "MULTI"

                title = (
                    legs[0]["display"]
                    if leg_count == 1
                    else f"Verified {leg_count}-Leg Parlay"
                )
                line_taken = legs[0]["line"] if leg_count == 1 else None
                bet_type = "straight" if leg_count == 1 else "parlay"
                verification_type = (
                    "verified"
                    if all(leg["verification_type"] == "verified" for leg in legs)
                    else "market"
                )

                cur.execute("""
                    INSERT INTO user_bets (
                        user_system_id, live_result_id, sportsbook,
                        odds_taken, line_taken, stake, units,
                        result, profit, bet_time, ticket_id, bet_type,
                        combined_odds, potential_profit, potential_return,
                        status, user_id, unit_value, bankroll_id,
                        official_combined_odds, user_combined_odds,
                        bankroll_balance_at_bet, unit_percentage,
                        sport, league, source, title, notes,
                        settled_at, is_manual, verification_type
                    )
                    VALUES (
                        NULL, NULL, %s, %s, %s, %s, %s,
                        NULL, NULL, NOW(), %s, %s, %s, %s, %s,
                        'pending', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, 'Prop-Line', %s, %s,
                        NULL, FALSE, %s
                    )
                    RETURNING id
                """, (
                    ticket_sportsbook,
                    combined_odds,
                    line_taken,
                    stake,
                    units,
                    str(ticket_id),
                    bet_type,
                    combined_odds,
                    potential_profit,
                    potential_return,
                    current_user.id,
                    unit_value,
                    bankroll_id,
                    combined_odds,
                    combined_odds,
                    current_balance,
                    unit_percentage,
                    ticket_sport,
                    ticket_league,
                    title,
                    notes or None,
                    verification_type,
                ))

                user_bet_id = cur.fetchone()[0]

                for sort_order, leg in enumerate(legs, start=1):
                    cur.execute("""
                        INSERT INTO user_bet_legs (
                            ticket_id, user_bet_id, player_name,
                            prop, ou, line, odds, sportsbook,
                            start_time, status, created_at,
                            official_sportsbook, official_odds,
                            user_sportsbook, user_odds,
                            official_line, user_line, sport, league,
                            selection_type, selection_name, team_name,
                            opponent, result, sort_order,
                            provider_market_id, provider_summary_id,
                            provider_event_id, provider_market_key,
                            verification_type
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, 'pending', NOW(), %s, %s, %s, %s,
                            %s, %s, %s, %s, 'Player Prop', %s,
                            NULL, %s, NULL, %s, %s, %s, %s, %s, %s
                        )
                    """, (
                        str(ticket_id),
                        user_bet_id,
                        leg["player_name"],
                        leg["market_key"],
                        leg["side"],
                        leg["line"],
                        leg["odds"],
                        leg["sportsbook"],
                        leg["event_start"],
                        leg["sportsbook"],
                        leg["odds"],
                        leg["sportsbook"],
                        leg["odds"],
                        leg["line"],
                        leg["line"],
                        leg["sport"],
                        leg["league"],
                        leg["player_name"],
                        leg["opponent"],
                        sort_order,
                        leg["provider_market_id"],
                        leg["summary_id"],
                        leg["provider_event_id"],
                        leg["market_key"],
                        leg["verification_type"],
                    ))

        flash(
            "Verified bet added."
            if len(legs) == 1
            else f"Verified {len(legs)}-leg ticket added.",
            "success"
        )

    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        print("Verified bet save error:", exc)
        flash("The verified ticket could not be saved.", "error")
    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))



@app.route("/my-hub/bets/add", methods=["POST"])
@login_required
def add_manual_bet():
    bankroll_id = request.form.get("bankroll_id", type=int)
    sport = clean_text(request.form.get("sport"))
    league = clean_text(request.form.get("league"))
    source = clean_text(request.form.get("source"))
    sportsbook = clean_text(request.form.get("sportsbook"))
    title = clean_text(request.form.get("title"))
    notes = clean_text(request.form.get("notes"))
    bet_time_raw = clean_text(request.form.get("bet_time"))

    selection_types = request.form.getlist("selection_type[]")
    selection_names = request.form.getlist("selection_name[]")
    props = request.form.getlist("prop[]")
    sides = request.form.getlist("ou[]")
    lines_raw = request.form.getlist("line[]")
    odds_raw = request.form.getlist("leg_odds[]")
    event_starts = request.form.getlist("event_start[]")

    try:
        stake = float(request.form.get("stake"))
    except (TypeError, ValueError):
        flash("Enter a valid stake.", "error")
        return redirect(url_for("my_bets_page"))

    leg_count = len(selection_names)

    if not bankroll_id:
        flash("Choose a bankroll.", "error")
        return redirect(url_for("my_bets_page"))

    if not sport or not sportsbook:
        flash("Sport and sportsbook are required.", "error")
        return redirect(url_for("my_bets_page"))

    if stake <= 0:
        flash("Stake must be greater than zero.", "error")
        return redirect(url_for("my_bets_page"))

    if leg_count < 1 or leg_count > 20:
        flash("Tickets must contain between 1 and 20 legs.", "error")
        return redirect(url_for("my_bets_page"))

    submitted_lists = [
        selection_types,
        props,
        sides,
        lines_raw,
        odds_raw,
        event_starts
    ]

    if any(len(items) != leg_count for items in submitted_lists):
        flash("The ticket legs were incomplete.", "error")
        return redirect(url_for("my_bets_page"))

    legs = []
    combined_decimal = 1.0

    for index in range(leg_count):
        selection_type = clean_text(selection_types[index])
        selection_name = clean_text(selection_names[index])
        prop = clean_text(props[index])
        ou = clean_text(sides[index]).lower()
        event_start = clean_text(event_starts[index])

        if not selection_type or not selection_name or not event_start:
            flash(f"Complete all required fields for leg {index + 1}.", "error")
            return redirect(url_for("my_bets_page"))

        try:
            odds = int(odds_raw[index])
        except (TypeError, ValueError):
            flash(f"Enter valid odds for leg {index + 1}.", "error")
            return redirect(url_for("my_bets_page"))

        if odds == 0:
            flash(f"Odds cannot be zero for leg {index + 1}.", "error")
            return redirect(url_for("my_bets_page"))

        line = None
        line_text = clean_text(lines_raw[index])

        if line_text:
            try:
                line = float(line_text)
            except ValueError:
                flash(f"Enter a valid line for leg {index + 1}.", "error")
                return redirect(url_for("my_bets_page"))

        decimal_odds = (
            1 + odds / 100
            if odds > 0
            else 1 + 100 / abs(odds)
        )

        combined_decimal *= decimal_odds

        legs.append({
            "selection_type": selection_type,
            "selection_name": selection_name,
            "prop": prop,
            "ou": ou,
            "line": line,
            "odds": odds,
            "event_start": event_start
        })

    if not title or title.lower() == "nan":
        title = generate_ticket_title(legs)

    combined_odds = (
        round((combined_decimal - 1) * 100)
        if combined_decimal >= 2
        else round(-100 / (combined_decimal - 1))
    )

    potential_profit = round(stake * (combined_decimal - 1), 2)
    potential_return = round(stake + potential_profit, 2)
    bet_type = "straight" if leg_count == 1 else "parlay"
    ticket_id = uuid.uuid4()

    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        current_balance,
                        unit_percentage
                    FROM user_bankrolls
                    WHERE id = %s
                      AND user_id = %s
                    FOR UPDATE
                """, (bankroll_id, current_user.id))

                bankroll = cur.fetchone()

                if not bankroll:
                    flash("That bankroll could not be found.", "error")
                    return redirect(url_for("my_bets_page"))

                current_balance = float(bankroll[1] or 0)
                unit_percentage = float(bankroll[2] or 0.01)
                unit_value = current_balance * unit_percentage
                units = stake / unit_value if unit_value > 0 else None

                straight_line = legs[0]["line"] if leg_count == 1 else None

                cur.execute("""
                    INSERT INTO user_bets (
                        user_system_id,
                        live_result_id,
                        sportsbook,
                        odds_taken,
                        line_taken,
                        stake,
                        units,
                        result,
                        profit,
                        bet_time,
                        ticket_id,
                        bet_type,
                        combined_odds,
                        potential_profit,
                        potential_return,
                        status,
                        user_id,
                        unit_value,
                        bankroll_id,
                        official_combined_odds,
                        user_combined_odds,
                        bankroll_balance_at_bet,
                        unit_percentage,
                        sport,
                        league,
                        source,
                        title,
                        notes,
                        settled_at,
                        is_manual
                    )
                    VALUES (
                        NULL, NULL, %s, %s, %s, %s, %s,
                        NULL, NULL, COALESCE(%s::timestamp, NOW()),
                        %s, %s, %s, %s, %s, 'pending',
                        %s, %s, %s, NULL, %s, %s, %s,
                        %s, %s, %s, %s, %s, NULL, TRUE
                    )
                    RETURNING id
                """, (
                    sportsbook,
                    combined_odds,
                    straight_line,
                    stake,
                    units,
                    bet_time_raw or None,
                    str(ticket_id),
                    bet_type,
                    combined_odds,
                    potential_profit,
                    potential_return,
                    current_user.id,
                    unit_value,
                    bankroll_id,
                    combined_odds,
                    current_balance,
                    unit_percentage,
                    sport,
                    league or None,
                    source or "Manual",
                    title or None,
                    notes or None
                ))

                user_bet_id = cur.fetchone()[0]

                for sort_order, leg in enumerate(legs, start=1):
                    cur.execute("""
                        INSERT INTO user_bet_legs (
                            ticket_id,
                            user_bet_id,
                            player_name,
                            prop,
                            ou,
                            line,
                            odds,
                            sportsbook,
                            start_time,
                            status,
                            created_at,
                            official_sportsbook,
                            official_odds,
                            user_sportsbook,
                            user_odds,
                            official_line,
                            user_line,
                            sport,
                            league,
                            selection_type,
                            selection_name,
                            team_name,
                            opponent,
                            result,
                            sort_order
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::timestamp, 'pending', NOW(),
                            NULL, NULL, %s, %s, NULL, %s,
                            %s, %s, %s, %s,
                            NULL, NULL, NULL, %s
                        )
                    """, (
                        str(ticket_id),
                        user_bet_id,
                        leg["selection_name"]
                        if leg["selection_type"] == "Player Prop"
                        else None,
                        leg["prop"] or leg["selection_type"],
                        leg["ou"] or None,
                        leg["line"],
                        leg["odds"],
                        sportsbook,
                        leg["event_start"],
                        sportsbook,
                        leg["odds"],
                        leg["line"],
                        sport,
                        league or None,
                        leg["selection_type"],
                        leg["selection_name"],
                        sort_order
                    ))

        flash(
            "Manual parlay added." if leg_count > 1 else "Manual bet added.",
            "success"
        )

    except Exception as exc:
        print("Manual ticket save error:", exc)
        flash("The ticket could not be saved.", "error")

    finally:
        conn.close()

    return redirect(url_for("my_bets_page"))

@app.route("/systems/<system_code>/watch", methods=["POST"])
@login_required
def toggle_system_watch(system_code):

    system_df = read_sql("""
        SELECT id
        FROM systems
        WHERE system_code = %s
        LIMIT 1
    """, (system_code,))

    if system_df.empty:
        return redirect(url_for("systems_page"))

    system_id = int(system_df.iloc[0]["id"])

    if is_watching_system(current_user.id, system_id):
        unwatch_system(current_user.id, system_id)
    else:
        watch_system(current_user.id, system_id)

    return redirect(url_for(
        "system_detail_page",
        system_code=system_code
    ))

@app.route("/systems/create", methods=["GET", "POST"])
@login_required
def create_system_page():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        sport = request.form.get("sport", "").strip().upper()
        visibility = request.form.get("visibility", "private").strip().lower()

        errors = []

        if len(name) < 3:
            errors.append("System name must be at least 3 characters.")

        if len(name) > 100:
            errors.append("System name cannot be longer than 100 characters.")

        if len(description) > 1000:
            errors.append("Description cannot be longer than 1,000 characters.")

        allowed_sports = {
            "MLB",
            "NBA",
            "NFL",
            "NHL"
        }

        if sport not in allowed_sports:
            errors.append("Please select a valid sport.")

        if visibility not in {"public", "private"}:
            errors.append("Please select a valid visibility option.")

        if errors:
            for error in errors:
                flash(error, "error")

            return render_template(
                "create_system.html",
                active_page="systems",
                form_data={
                    "name": name,
                    "description": description,
                    "sport": sport,
                    "visibility": visibility
                }
            )

        base_code = re.sub(
            r"[^a-z0-9]+",
            "_",
            name.lower()
        ).strip("_")

        if not base_code:
            base_code = "system"

        # systems.system_code is VARCHAR(20)
        MAX_SYSTEM_CODE_LENGTH = 20

        base_code = base_code[:MAX_SYSTEM_CODE_LENGTH].rstrip("_")
        system_code = base_code
        number = 2

        while system_code_exists(system_code):
            suffix = f"_{number}"

            system_code = (
                base_code[:MAX_SYSTEM_CODE_LENGTH - len(suffix)].rstrip("_")
                + suffix
    )
            number += 1        
    
        created_system_code = create_system_record(
            system_code=system_code,
            name=name,
            description=description,
            creator_id=current_user.id,
            sport=sport,
            visibility=visibility
        )

        flash("Your system was created successfully.", "success")

        return redirect(url_for(
            "system_detail_page",
            system_code=created_system_code
        ))

    return render_template(
        "create_system.html",
        active_page="systems",
        form_data={}
    )

@app.route("/systems/save-combo", methods=["POST"])
@login_required
def save_combo_system():

    data = request.get_json(silent=True)

    if not data:
        return {
            "success": False,
            "message": "No combo data was received."
        }, 400

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    sport = str(data.get("sport", "MLB")).strip()
    visibility = str(data.get("visibility", "private")).strip().lower()
    combo_name = str(data.get("combo_name", name)).strip()

    minimum_combined_odds = data.get("minimum_combined_odds")
    require_all_active = bool(data.get("require_all_active", True))
    require_exact_lines = bool(data.get("require_exact_lines", True))
    legs = data.get("legs", [])

    if not name:
        return {
            "success": False,
            "message": "System name is required."
        }, 400

    if visibility not in ("public", "private"):
        visibility = "private"

    if not isinstance(legs, list) or len(legs) < 2:
        return {
            "success": False,
            "message": "A combo system must contain at least two legs."
        }, 400

    cleaned_legs = []

    for index, leg in enumerate(legs, start=1):

        if not isinstance(leg, dict):
            return {
                "success": False,
                "message": f"Leg {index} is invalid."
            }, 400

        player_name = str(leg.get("player_name", "")).strip()
        prop = str(leg.get("prop", "")).strip().upper()
        ou = str(leg.get("ou", "over")).strip().lower()
        line = leg.get("line")

        if not player_name or not prop or line is None:
            return {
                "success": False,
                "message": f"Leg {index} is missing required information."
            }, 400

        try:
            line = float(line)
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": f"Leg {index} has an invalid line."
            }, 400

        if ou not in ("over", "under"):
            ou = "over"

        cleaned_legs.append({
            "player_name": player_name,
            "prop": prop,
            "ou": ou,
            "line": line
        })

    if minimum_combined_odds in ("", None):
        minimum_combined_odds = None
    else:
        try:
            minimum_combined_odds = int(minimum_combined_odds)
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": "Minimum combined odds must be a whole number."
            }, 400

    base_code = re.sub(
        r"[^a-z0-9]+",
        "_",
        name.lower()
    ).strip("_")

    if not base_code:
        base_code = "combo_system"

    # systems.system_code is VARCHAR(20)
    MAX_SYSTEM_CODE_LENGTH = 20

    base_code = base_code[:MAX_SYSTEM_CODE_LENGTH].rstrip("_")
    
    if not base_code:
        base_code = "combo_system"

    system_code = base_code
    number = 2

    while system_code_exists(system_code):
        suffix = f"_{number}"

        system_code = (
            base_code[:MAX_SYSTEM_CODE_LENGTH - len(suffix)].rstrip("_")
            + suffix
    )

        number += 1

    try:
        created_system_code = create_combo_system(
            system_code=system_code,
            name=name,
            description=description,
            creator_id=current_user.id,
            sport=sport,
            visibility=visibility,
            combo_name=combo_name or name,
            minimum_combined_odds=minimum_combined_odds,
            require_all_active=require_all_active,
            require_exact_lines=require_exact_lines,
            legs=cleaned_legs
        )

        return {
            "success": True,
            "message": "Combo system saved.",
            "system_code": created_system_code,
            "redirect_url": url_for(
                "system_detail_page",
                system_code=created_system_code
            )
        }

    except Exception as exc:
        app.logger.exception("Failed to save combo system")

        return {
            "success": False,
            "message": "The combo could not be saved."
        }, 500

@app.route("/share")
def share_page():
    context = get_common_context(active_page="share")
    return render_template("share.html", **context)

@app.route("/upgrade")
def upgrade_page():
    required = request.args.get("required", "premium")

    plan_names = {
        "premium": "Premium",
        "premium_plus": "Premium Plus",
        "capper": "Capper Access",
        "beta": "Beta Tester Access"
    }

    return render_template(
        "upgrade.html",
        required=required,
        required_name=plan_names.get(required, "Premium"),
        active_page=""
    )


@app.route("/clear_cache")
def clear_cache():
    cache.clear()
    return "Cache cleared"

@app.route("/my-hub/bets/sync-results", methods=["POST"])
@login_required
def sync_personal_bet_results():
    try:
        # ---------------------------------------------------------
        # PASS 1: grade currently-pending verified bets
        # ---------------------------------------------------------
        scan = grade_pending_mlb_bets(
            user_id=current_user.id
        )

        app.logger.warning(
            "AUTO GRADING SCAN user=%s graded=%s skipped=%s ready=%s",
            current_user.id,
            scan.get("graded_legs"),
            scan.get("skipped_legs"),
            scan.get("ready_tickets"),
        )

        settled = 0
        failed = 0

        for ticket in scan["ready_tickets"]:
            response = grade_bet(
                bet_id=ticket["bet_id"],
                result=ticket["result"],
                user_id=current_user.id,
                update_legs=False,
                odds_override=ticket.get("adjusted_odds")
            )

            if response.get("success"):
                settled += 1
            else:
                failed += 1

        # ---------------------------------------------------------
        # PASS 2: reconcile already-settled VERIFIED tickets
        # against official provider_results.
        #
        # Supports straight bets AND parlays.
        # ---------------------------------------------------------
        corrected = 0
        correction_failed = 0

        conn = get_conn()

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ub.id AS bet_id,
                        LOWER(COALESCE(ub.result, ub.status, '')) AS bet_result,
                        LOWER(COALESCE(ub.bet_type, 'straight')) AS bet_type,
                        ubl.id AS leg_id,
                        LOWER(COALESCE(
                            ubl.result,
                            ubl.status,
                            'pending'
                        )) AS stored_leg_result,
                        ubl.player_name,
                        ubl.provider_event_id,
                        ubl.provider_market_key,
                        ubl.ou,
                        COALESCE(ubl.user_line, ubl.line) AS line,
                        COALESCE(
                            ubl.user_odds,
                            ubl.odds,
                            ubl.official_odds
                        ) AS leg_odds
                    FROM user_bets ub
                    JOIN user_bet_legs ubl
                      ON ubl.user_bet_id = ub.id
                    WHERE ub.user_id = %s
                      AND UPPER(COALESCE(ub.sport, '')) = 'MLB'
                      AND LOWER(COALESCE(ub.status, 'pending'))
                            IN ('won', 'lost', 'push', 'void')
                      AND ubl.provider_event_id IS NOT NULL
                      AND ubl.provider_market_key IS NOT NULL
                    ORDER BY
                        ub.id,
                        COALESCE(ubl.sort_order, ubl.id)
                """, (current_user.id,))

                rows = cur.fetchall()

                bets = {}

                for row in rows:
                    (
                        bet_id,
                        bet_result,
                        bet_type,
                        leg_id,
                        stored_leg_result,
                        player_name,
                        provider_event_id,
                        provider_market_key,
                        side,
                        line,
                        leg_odds
                    ) = row

                    item = bets.setdefault(
                        int(bet_id),
                        {
                            "bet_result": str(
                                bet_result or ""
                            ).strip().lower(),
                            "bet_type": str(
                                bet_type or "straight"
                            ).strip().lower(),
                            "legs": []
                        }
                    )

                    item["legs"].append({
                        "leg_id": int(leg_id),
                        "stored_result": str(
                            stored_leg_result or "pending"
                        ).strip().lower(),
                        "player_name": player_name,
                        "provider_event_id": provider_event_id,
                        "provider_market_key": provider_market_key,
                        "side": side,
                        "line": line,
                        "odds": (
                            int(leg_odds)
                            if leg_odds is not None
                            else None
                        ),
                    })

                correction_plans = []

                for bet_id, bet_data in bets.items():
                    provider_legs = []
                    unresolved = False

                    for leg in bet_data["legs"]:
                        side = str(
                            leg["side"] or ""
                        ).strip()

                        market_key = str(
                            leg["provider_market_key"] or ""
                        ).strip()

                        if not side or not market_key:
                            unresolved = True
                            break

                        if leg["line"] is None:
                            cur.execute("""
                                SELECT
                                    LOWER(TRIM(resolution))
                                FROM provider_results
                                WHERE provider = 'prop_line'
                                  AND provider_event_id = %s
                                  AND market_key = %s
                                  AND LOWER(TRIM(player_name))
                                        = LOWER(TRIM(%s))
                                  AND LOWER(TRIM(outcome_name))
                                        = LOWER(TRIM(%s))
                                  AND line IS NULL
                                  AND resolution IS NOT NULL
                                  AND COALESCE(redacted, FALSE) = FALSE
                                ORDER BY resolved_at DESC, id DESC
                            """, (
                                str(leg["provider_event_id"]),
                                market_key,
                                leg["player_name"],
                                side,
                            ))
                        else:
                            cur.execute("""
                                SELECT
                                    LOWER(TRIM(resolution))
                                FROM provider_results
                                WHERE provider = 'prop_line'
                                  AND provider_event_id = %s
                                  AND market_key = %s
                                  AND LOWER(TRIM(player_name))
                                        = LOWER(TRIM(%s))
                                  AND LOWER(TRIM(outcome_name))
                                        = LOWER(TRIM(%s))
                                  AND line = %s
                                  AND resolution IS NOT NULL
                                  AND COALESCE(redacted, FALSE) = FALSE
                                ORDER BY resolved_at DESC, id DESC
                            """, (
                                str(leg["provider_event_id"]),
                                market_key,
                                leg["player_name"],
                                side,
                                leg["line"],
                            ))

                        resolutions = {
                            str(result_row[0] or "")
                            .strip()
                            .lower()
                            for result_row in cur.fetchall()
                            if str(result_row[0] or "")
                            .strip()
                            .lower()
                            in {"won", "lost", "push", "void"}
                        }

                        # Missing or conflicting provider outcomes:
                        # do not alter this ticket.
                        if len(resolutions) != 1:
                            unresolved = True
                            break

                        provider_leg_result = next(iter(resolutions))

                        provider_legs.append({
                            **leg,
                            "provider_result": provider_leg_result,
                        })

                    if unresolved or not provider_legs:
                        continue

                    statuses = [
                        leg["provider_result"]
                        for leg in provider_legs
                    ]

                    ticket_result = None
                    adjusted_odds = None

                    if "lost" in statuses:
                        ticket_result = "lost"

                    elif statuses and all(
                        status in {"push", "void"}
                        for status in statuses
                    ):
                        ticket_result = "push"

                    elif statuses and all(
                        status in {"won", "push", "void"}
                        for status in statuses
                    ):
                        ticket_result = "won"

                        removed_legs = [
                            leg
                            for leg in provider_legs
                            if leg["provider_result"] in {"push", "void"}
                        ]

                        # A reduced parlay needs recalculated odds.
                        if removed_legs:
                            active_winners = [
                                leg
                                for leg in provider_legs
                                if leg["provider_result"] == "won"
                            ]

                            if (
                                not active_winners
                                or any(
                                    leg["odds"] in (None, 0)
                                    for leg in active_winners
                                )
                            ):
                                continue

                            combined_decimal = 1.0

                            for leg in active_winners:
                                odds = int(leg["odds"])

                                if odds > 0:
                                    decimal_odds = 1 + odds / 100
                                else:
                                    decimal_odds = (
                                        1 + 100 / abs(odds)
                                    )

                                combined_decimal *= decimal_odds

                            if combined_decimal <= 1:
                                continue

                            if combined_decimal >= 2:
                                adjusted_odds = round(
                                    (combined_decimal - 1) * 100
                                )
                            else:
                                adjusted_odds = round(
                                    -100 / (combined_decimal - 1)
                                )

                    if ticket_result is None:
                        continue

                    leg_results = [
                        {
                            "leg_id": leg["leg_id"],
                            "result": leg["provider_result"],
                        }
                        for leg in provider_legs
                    ]

                    leg_changed = any(
                        leg["stored_result"]
                        != leg["provider_result"]
                        for leg in provider_legs
                    )

                    ticket_changed = (
                        bet_data["bet_result"] != ticket_result
                    )

                    # Even if ticket result stays the same, correct any
                    # individual leg that disagrees with provider_results.
                    if not ticket_changed and not leg_changed:
                        continue

                    correction_plans.append({
                        "bet_id": bet_id,
                        "stored_result": bet_data["bet_result"],
                        "provider_result": ticket_result,
                        "bet_type": bet_data["bet_type"],
                        "leg_results": leg_results,
                        "adjusted_odds": adjusted_odds,
                    })

        finally:
            conn.close()

        # Run each correction in its own atomic regrade transaction.
        for plan in correction_plans:
            app.logger.warning(
                "BET RECONCILE user=%s bet=%s type=%s "
                "stored=%s provider=%s legs=%s adjusted_odds=%s",
                current_user.id,
                plan["bet_id"],
                plan["bet_type"],
                plan["stored_result"],
                plan["provider_result"],
                plan["leg_results"],
                plan["adjusted_odds"],
            )

            correction = regrade_bet(
                bet_id=plan["bet_id"],
                result=plan["provider_result"],
                user_id=current_user.id,
                update_legs=False,
                odds_override=plan["adjusted_odds"],
                reason=(
                    "Official Prop-Line provider_results "
                    "ticket reconciliation"
                ),
                leg_results=plan["leg_results"],
            )

            if correction.get("success"):
                corrected += 1
            else:
                correction_failed += 1

                app.logger.error(
                    "BET RECONCILE FAILED "
                    "user=%s bet=%s error=%s",
                    current_user.id,
                    plan["bet_id"],
                    correction.get("error"),
                )

        graded_legs = len(scan["graded_legs"])
        skipped_legs = len(scan["skipped_legs"])

        if corrected or settled:
            parts = []

            if graded_legs:
                parts.append(
                    f"{graded_legs} leg"
                    f"{'' if graded_legs == 1 else 's'} graded"
                )

            if settled:
                parts.append(
                    f"{settled} ticket"
                    f"{'' if settled == 1 else 's'} settled"
                )

            if corrected:
                parts.append(
                    f"{corrected} ticket"
                    f"{'' if corrected == 1 else 's'} corrected"
                )

            if skipped_legs:
                parts.append(
                    f"{skipped_legs} leg"
                    f"{'' if skipped_legs == 1 else 's'} still pending"
                )

            flash(
                "Results synced: " + " · ".join(parts),
                "success"
            )

        elif failed or correction_failed:
            flash(
                "Results were found, but one or more settlements "
                "or corrections failed.",
                "error"
            )

        elif graded_legs:
            flash(
                f"{graded_legs} leg"
                f"{'' if graded_legs == 1 else 's'} graded. "
                "The remaining ticket legs are still pending.",
                "info"
            )

        else:
            flash(
                "No pending MLB legs or settlement corrections were found.",
                "info"
            )

    except Exception:
        app.logger.exception(
            "Personal Hub result sync failed"
        )

        flash(
            "Result sync failed. No tickets were settled or corrected.",
            "error"
        )

    return redirect(url_for("my_bets_page"))
@app.route("/my-hub/bets/<int:bet_id>/grade", methods=["POST"])
@login_required
def grade_personal_bet(bet_id):
    result = clean_text(
        request.form.get("result")
    ).lower()

    response = grade_bet(
        bet_id=bet_id,
        result=result,
        user_id=current_user.id
    )

    if response["success"]:
        profit = response["profit"]

        if profit > 0:
            profit_text = f"+${profit:.2f}"
        elif profit < 0:
            profit_text = f"-${abs(profit):.2f}"
        else:
            profit_text = "$0.00"

        flash(
            f"Bet graded {result.upper()} · "
            f"{profit_text}",
            "success"
        )

    else:
        flash(
            response.get(
                "error",
                "Bet could not be graded."
            ),
            "error"
        )

    return redirect(
        url_for("my_bets_page")
    )

@app.route("/strategy-finder")
@premium_required
def strategy_finder():
    prop = request.args.get("prop", "HR")
    odds_min = request.args.get("odds_min", "")
    odds_max = request.args.get("odds_max", "")
    window = request.args.get("window", "30")
    checkpoint = request.args.get("checkpoint", "close")

    day_night = request.args.get("day_night", "")
    home_away = request.args.get("home_away", "")
    team = request.args.get("team", "")
    vs_team = request.args.get("vs_team", "")

    results = None

    if odds_min or odds_max:
        conn = get_conn()

        stat_map = {
            "HR": "home_runs",
            "HITS": "hits",
            "TB": "total_bases",
            "RBI": "rbi",
            "RUNS": "runs",
        }

        stat_col = stat_map.get(prop, "home_runs")

        allowed_checkpoints = ["open", "12h", "3h", "close"]

        if checkpoint not in allowed_checkpoints:
            checkpoint = "close"

        sql = f"""
            SELECT
                o.player,
                o.prop,
                o.ou,
                o.line,
                o.odds,
                o.sportsbook,
                o.game_date,
                h.team,
                h.opponent,
                h.is_home,
                h.{stat_col} AS result_stat
            FROM odds_market_timeline o
            JOIN mlb_hitter_gamelogs h
              ON LOWER(TRIM(h.player_name)) = LOWER(TRIM(o.player))
             AND h.game_date = o.game_date
            WHERE o.prop = %s
              AND LOWER(o.ou) = 'over'
              AND o.checkpoint = %s
              AND o.odds IS NOT NULL
              AND o.line IS NOT NULL
              AND o.game_date >= CURRENT_DATE - (%s || ' days')::interval
        """

        params = [prop, checkpoint, window]

        if odds_min:
            sql += " AND o.odds >= %s"
            params.append(int(odds_min))

        if odds_max:
            sql += " AND o.odds <= %s"
            params.append(int(odds_max))

        if team:
            sql += " AND UPPER(h.team) = %s"
            params.append(team.upper().strip())

        if vs_team:
            sql += " AND UPPER(h.opponent) = %s"
            params.append(vs_team.upper().strip())

        if home_away == "home":
            sql += " AND h.is_home = TRUE"

        if home_away == "away":
            sql += " AND h.is_home = FALSE"

        sql += " LIMIT 10000"

        # ============================================================
        # LEGACY STRATEGY FINDER QUERY
        # ============================================================
        df = pd.read_sql(sql, conn, params=params)

        # ============================================================
        # DATA FOUNDATION SHADOW QUERY
        # Provider-backed equivalent of odds_market_timeline.
        #
        # IMPORTANT:
        # This does NOT affect what the user sees yet.
        # It only compares legacy vs provider data in the logs.
        # ============================================================
        provider_df = pd.DataFrame()

        try:
            market = resolve_market(display_prop=prop)

            if market:
                provider_sql = f"""
                    SELECT
                        pmh.player_name AS player,
                        pmh.market_key AS prop,
                        pmh.outcome_name AS ou,
                        pmh.line,
                        pmh.odds,
                        pmh.bookmaker_key AS sportsbook,
                        (
                            pe.commence_time
                            AT TIME ZONE 'America/New_York'
                        )::date AS game_date,
                        h.team,
                        h.opponent,
                        h.is_home,
                        h.{stat_col} AS result_stat
                    FROM provider_market_history pmh
                    JOIN provider_events pe
                      ON pe.provider = pmh.provider
                     AND pe.provider_event_id = pmh.provider_event_id
                    JOIN mlb_hitter_gamelogs h
                      ON LOWER(TRIM(h.player_name)) = LOWER(TRIM(pmh.player_name))
                     AND h.game_date = (
                            pe.commence_time
                            AT TIME ZONE 'America/New_York'
                         )::date
                    WHERE pmh.sport_key = 'baseball_mlb'
                      AND pmh.market_key = %s
                      AND LOWER(TRIM(pmh.outcome_name)) = 'over'
                      AND pmh.checkpoint = %s
                      AND pmh.odds IS NOT NULL
                      AND pmh.line IS NOT NULL
                      AND (
                            pe.commence_time
                            AT TIME ZONE 'America/New_York'
                          )::date >= CURRENT_DATE - (%s || ' days')::interval
                """

                provider_params = [
                    market.key,
                    checkpoint,
                    window,
                ]

                if odds_min:
                    provider_sql += " AND pmh.odds >= %s"
                    provider_params.append(int(odds_min))

                if odds_max:
                    provider_sql += " AND pmh.odds <= %s"
                    provider_params.append(int(odds_max))

                if team:
                    provider_sql += " AND UPPER(h.team) = %s"
                    provider_params.append(team.upper().strip())

                if vs_team:
                    provider_sql += " AND UPPER(h.opponent) = %s"
                    provider_params.append(vs_team.upper().strip())

                if home_away == "home":
                    provider_sql += " AND h.is_home = TRUE"

                if home_away == "away":
                    provider_sql += " AND h.is_home = FALSE"

                # Keep the same cap as the legacy query for the first comparison.
                provider_sql += " LIMIT 10000"

                provider_df = pd.read_sql(
                    provider_sql,
                    conn,
                    params=provider_params
                )

                # Match the legacy grading behavior exactly during shadow testing.
                # Push handling will be corrected after provider parity is proven.
                provider_bets = len(provider_df)

                if provider_bets > 0:
                    provider_df["result_status"] = provider_df.apply(
                        lambda r: (
                            "Won"
                            if float(r["result_stat"]) > float(r["line"])
                            else "Lost"
                        ),
                        axis=1
                    )

                    provider_wins = int(
                        (provider_df["result_status"] == "Won").sum()
                    )
                    provider_losses = int(
                        (provider_df["result_status"] == "Lost").sum()
                    )

                    provider_profit = 0.0

                    for _, provider_row in provider_df.iterrows():
                        provider_odds = int(provider_row["odds"])

                        if provider_row["result_status"] == "Won":
                            if provider_odds > 0:
                                provider_profit += provider_odds / 100
                            else:
                                provider_profit += 100 / abs(provider_odds)
                        else:
                            provider_profit -= 1

                    provider_roi = (
                        provider_profit / provider_bets
                    ) * 100
                else:
                    provider_wins = 0
                    provider_losses = 0
                    provider_profit = 0.0
                    provider_roi = 0.0

                app.logger.warning(
                    "\n"
                    "====================================================\n"
                    "STRATEGY FINDER DATA FOUNDATION SHADOW TEST\n"
                    "====================================================\n"
                    "PROP: %s\n"
                    "PROVIDER MARKET: %s\n"
                    "CHECKPOINT: %s\n"
                    "WINDOW: %s\n"
                    "ODDS: %s to %s\n"
                    "----------------------------------------------------\n"
                    "LEGACY ROWS:   %s\n"
                    "PROVIDER ROWS: %s\n"
                    "----------------------------------------------------\n"
                    "PROVIDER WINS:   %s\n"
                    "PROVIDER LOSSES: %s\n"
                    "PROVIDER UNITS:  %.2f\n"
                    "PROVIDER ROI:    %.2f%%%%\n"
                    "====================================================",
                    prop,
                    market.key,
                    checkpoint,
                    window,
                    odds_min or "ANY",
                    odds_max or "ANY",
                    len(df),
                    provider_bets,
                    provider_wins,
                    provider_losses,
                    provider_profit,
                    provider_roi,
                )

            else:
                app.logger.warning(
                    "Strategy Finder shadow test: "
                    "could not resolve market for prop=%s",
                    prop
                )

        except Exception:
            app.logger.exception(
                "Strategy Finder provider shadow query failed"
            )

        conn.close()

        # Existing Strategy Finder continues to use the legacy dataframe
        # until provider parity has been validated.
        bets = len(df)

        if bets > 0:
            df["result_status"] = df.apply(
                lambda r: "Won" if float(r["result_stat"]) > float(r["line"]) else "Lost",
                axis=1
            )

            wins = int((df["result_status"] == "Won").sum())
            losses = int((df["result_status"] == "Lost").sum())

            profit = 0

            for _, row in df.iterrows():
                odds = int(row["odds"])

                if row["result_status"] == "Won":
                    if odds > 0:
                        profit += odds / 100
                    else:
                        profit += 100 / abs(odds)
                else:
                    profit -= 1

            roi = (profit / bets) * 100

            results = {
                "bets": bets,
                "wins": wins,
                "losses": losses,
                "hit_rate": round((wins / bets) * 100, 1),
                "units": round(profit, 2),
                "roi": round(roi, 1),
            }
        else:
            results = {
                "bets": 0,
                "wins": 0,
                "losses": 0,
                "hit_rate": 0,
                "units": 0,
                "roi": 0,
            }

    return render_template(
        "strategy_finder.html",
        active_page="strategy_finder",
        prop=prop,
        odds_min=odds_min,
        odds_max=odds_max,
        window=window,
        checkpoint=checkpoint,
        day_night=day_night,
        home_away=home_away,
        team=team,
        vs_team=vs_team,
        results=results
    )

if __name__ == "__main__":
    app.run(debug=True)
