from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from database import get_conn


TORONTO = ZoneInfo("America/Toronto")


HITTER_PROP_ALIASES = {
    "HIT": "hits",
    "HITS": "hits",
    "TOTAL BASE": "total_bases",
    "TOTAL BASES": "total_bases",
    "TB": "total_bases",
    "HOME RUN": "home_runs",
    "HOME RUNS": "home_runs",
    "HR": "home_runs",
    "RUN": "runs",
    "RUNS": "runs",
    "RBI": "rbi",
    "RBIS": "rbi",
}

PITCHER_PROP_ALIASES = {
    "STRIKEOUT": "strikeouts",
    "STRIKEOUTS": "strikeouts",
    "PITCHER STRIKEOUTS": "strikeouts",
    "SO": "strikeouts",
    "K": "strikeouts",
    "KS": "strikeouts",
}


def normalize_prop(value):
    return " ".join(
        str(value or "")
        .upper()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def compare_result(stat_value, side, line):
    side = str(side or "").strip().lower()

    if line is None:
        return None

    stat_value = float(stat_value)
    line = float(line)

    if stat_value == line:
        return "push"

    if side in {"over", "yes"}:
        return "won" if stat_value > line else "lost"

    if side in {"under", "no"}:
        return "won" if stat_value < line else "lost"

    return None


def local_event_date(start_time):
    if start_time is None:
        return None

    if getattr(start_time, "tzinfo", None) is not None:
        return start_time.astimezone(TORONTO).date()

    return start_time.date()


def safe_to_grade(event_date):
    """
    Phase 1 safety rule:
    only auto-grade events dated before today in Toronto.

    This avoids settling from incomplete same-day box scores until
    a true final-status feed is connected.
    """
    if not event_date:
        return False

    return event_date < datetime.now(TORONTO).date()


def get_single_hitter_result(cur, player_name, event_date, stat_key):
    stat_sql = {
        "hits": "COALESCE(SUM(h), 0)",
        "total_bases": "COALESCE(SUM(tb), 0)",
        "home_runs": "COALESCE(SUM(hr), 0)",
        "runs": "COALESCE(SUM(runs_scored), 0)",
        "rbi": "COALESCE(SUM(rbi), 0)",
    }.get(stat_key)

    if not stat_sql:
        return None, "unsupported_prop"

    cur.execute(f"""
        SELECT
            game_date,
            {stat_sql} AS stat_value,
            COUNT(DISTINCT COALESCE(gamepk::text, game_date::text)) AS game_count
        FROM mlb_pa_gamelog
        WHERE LOWER(TRIM(batter_name)) = LOWER(TRIM(%s))
          AND game_date = %s
        GROUP BY game_date
    """, (player_name, event_date))

    rows = cur.fetchall()

    if not rows:
        return None, "no_stats"

    row = rows[0]

    # Without a saved game_id, doubleheaders are ambiguous.
    if int(row[2] or 0) > 1:
        return None, "ambiguous_doubleheader"

    return float(row[1] or 0), None


def get_single_pitcher_result(cur, player_name, event_date, stat_key):
    column = {
        "strikeouts": "strikeouts",
    }.get(stat_key)

    if not column:
        return None, "unsupported_prop"

    cur.execute(f"""
        SELECT
            {column} AS stat_value,
            COUNT(*) OVER () AS game_count
        FROM mlb_pitcher_gamelogs
        WHERE LOWER(TRIM(player_name)) = LOWER(TRIM(%s))
          AND game_date = %s
        ORDER BY id
    """, (player_name, event_date))

    rows = cur.fetchall()

    if not rows:
        return None, "no_stats"

    if len(rows) > 1:
        return None, "ambiguous_doubleheader"

    return float(rows[0][0] or 0), None


def grade_pending_mlb_straights(user_id):
    """
    Match and grade pending one-leg MLB tickets.

    Returns leg decisions only. The Flask route uses the existing
    grade_bet() service to settle bankrolls and transactions.
    """
    conn = get_conn()

    graded = []
    skipped = []

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ub.id AS bet_id,
                        ubl.id AS leg_id,
                        COALESCE(
                            ubl.selection_name,
                            ubl.player_name,
                            ubl.team_name
                        ) AS selection_name,
                        ubl.prop,
                        ubl.ou,
                        COALESCE(ubl.user_line, ubl.line) AS line,
                        ubl.start_time,
                        ub.bet_type,
                        ub.sport
                    FROM user_bets ub
                    JOIN user_bet_legs ubl
                        ON ubl.user_bet_id = ub.id
                    WHERE ub.user_id = %s
                      AND LOWER(COALESCE(ub.status, 'pending')) = 'pending'
                      AND LOWER(COALESCE(ub.bet_type, 'straight')) = 'straight'
                      AND UPPER(COALESCE(ub.sport, '')) = 'MLB'
                      AND LOWER(COALESCE(ubl.status, 'pending')) = 'pending'
                    ORDER BY ub.id
                """, (user_id,))

                rows = cur.fetchall()

                for row in rows:
                    bet_id = int(row[0])
                    leg_id = int(row[1])
                    player_name = row[2]
                    raw_prop = row[3]
                    side = row[4]
                    line = row[5]
                    start_time = row[6]

                    event_date = local_event_date(start_time)

                    if not safe_to_grade(event_date):
                        skipped.append({
                            "bet_id": bet_id,
                            "reason": "not_final_window"
                        })
                        continue

                    prop_key = normalize_prop(raw_prop)

                    if prop_key in HITTER_PROP_ALIASES:
                        stat_value, error = get_single_hitter_result(
                            cur,
                            player_name,
                            event_date,
                            HITTER_PROP_ALIASES[prop_key]
                        )
                    elif prop_key in PITCHER_PROP_ALIASES:
                        stat_value, error = get_single_pitcher_result(
                            cur,
                            player_name,
                            event_date,
                            PITCHER_PROP_ALIASES[prop_key]
                        )
                    else:
                        skipped.append({
                            "bet_id": bet_id,
                            "reason": "unsupported_prop"
                        })
                        continue

                    if error:
                        skipped.append({
                            "bet_id": bet_id,
                            "reason": error
                        })
                        continue

                    result = compare_result(
                        stat_value,
                        side,
                        line
                    )

                    if result is None:
                        skipped.append({
                            "bet_id": bet_id,
                            "reason": "unsupported_side_or_line"
                        })
                        continue

                    cur.execute("""
                        UPDATE user_bet_legs
                        SET
                            result = %s,
                            status = %s
                        WHERE id = %s
                          AND user_bet_id = %s
                    """, (
                        result,
                        result,
                        leg_id,
                        bet_id
                    ))

                    graded.append({
                        "bet_id": bet_id,
                        "leg_id": leg_id,
                        "result": result,
                        "stat_value": stat_value,
                        "line": float(line),
                        "player_name": player_name,
                        "prop": raw_prop
                    })

        return {
            "success": True,
            "graded": graded,
            "skipped": skipped
        }

    finally:
        conn.close()
