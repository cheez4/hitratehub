from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from database import get_conn
from services.market_registry import resolve_market

TORONTO = ZoneInfo("America/Toronto")
FINAL_EVENT_STATUSES = {"final", "completed", "complete"}

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

def provider_event_is_final(cur, provider_event_id):
    if not provider_event_id:
        return False, "missing_provider_event_id"
    cur.execute("""
        SELECT LOWER(COALESCE(status, ''))
        FROM provider_events
        WHERE provider = 'prop_line'
          AND provider_event_id = %s
        LIMIT 1
    """, (str(provider_event_id),))
    row = cur.fetchone()
    if not row:
        return False, "provider_event_missing"
    status = str(row[0] or "").strip().lower()
    if status in FINAL_EVENT_STATUSES:
        return True, None
    return False, f"event_not_final:{status or 'unknown'}"

def innings_pitched_to_outs(value):
    if value is None:
        return None
    try:
        dec = Decimal(str(value).strip())
    except Exception:
        return None
    whole = int(dec)
    frac = float(dec - Decimal(whole))
    if abs(frac - 0.0) < 0.02:
        extra = 0
    elif abs(frac - 0.1) < 0.02:
        extra = 1
    elif abs(frac - 0.2) < 0.02:
        extra = 2
    elif abs(frac - (1/3)) < 0.02:
        extra = 1
    elif abs(frac - (2/3)) < 0.02:
        extra = 2
    else:
        return None
    return whole * 3 + extra

def get_single_hitter_result(cur, player_name, event_date, market):
    stat_sql = {
        "h": "COALESCE(SUM(h), 0)",
        "tb": "COALESCE(SUM(tb), 0)",
        "hr": "COALESCE(SUM(hr), 0)",
        "runs_scored": "COALESCE(SUM(runs_scored), 0)",
        "rbi": "COALESCE(SUM(rbi), 0)",
        "h+runs_scored+rbi": "COALESCE(SUM(h),0)+COALESCE(SUM(runs_scored),0)+COALESCE(SUM(rbi),0)",
    }.get(market.source)
    if not stat_sql:
        return None, "unsupported_hitter_stat"
    cur.execute(f"""
        SELECT {stat_sql} AS stat_value,
               COUNT(DISTINCT COALESCE(gamepk::text, game_date::text)) AS game_count
        FROM mlb_pa_gamelog
        WHERE LOWER(TRIM(batter_name)) = LOWER(TRIM(%s))
          AND game_date = %s
    """, (player_name, event_date))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None, "no_stats"
    if int(row[1] or 0) > 1:
        return None, "ambiguous_doubleheader"
    return float(row[0] or 0), None

def get_single_pitcher_result(cur, player_name, event_date, market):
    source = market.source
    if source == "outs_from_innings":
        cur.execute("""
            SELECT innings_pitched
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
        outs = innings_pitched_to_outs(rows[0][0])
        if outs is None:
            return None, "invalid_innings_pitched"
        return float(outs), None

    allowed = {"strikeouts","earned_runs","hits_allowed","walks_allowed","runs_allowed"}
    if source not in allowed:
        return None, "unsupported_pitcher_stat"

    cur.execute(f"""
        SELECT {source}
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

def grade_pending_mlb_bets(user_id):
    conn = get_conn()
    graded, skipped = [], []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ub.id,
                        ubl.id,
                        COALESCE(ubl.selection_name, ubl.player_name, ubl.team_name),
                        ubl.prop,
                        ubl.ou,
                        COALESCE(ubl.user_line, ubl.line),
                        ubl.start_time,
                        ub.bet_type,
                        ub.sport,
                        ubl.provider_event_id,
                        ubl.provider_market_key
                    FROM user_bets ub
                    JOIN user_bet_legs ubl ON ubl.user_bet_id = ub.id
                    WHERE ub.user_id = %s
                      AND LOWER(COALESCE(ub.status,'pending')) = 'pending'
                      AND UPPER(COALESCE(ub.sport,'')) = 'MLB'
                      AND LOWER(COALESCE(ubl.status,'pending')) = 'pending'
                    ORDER BY ub.id, COALESCE(ubl.sort_order, ubl.id)
                """, (user_id,))
                rows = cur.fetchall()

                for row in rows:
                    bet_id, leg_id = int(row[0]), int(row[1])
                    player_name, raw_prop, side, line, start_time = row[2], row[3], row[4], row[5], row[6]
                    provider_event_id, provider_market_key = row[9], row[10]

                    final, final_error = provider_event_is_final(cur, provider_event_id)
                    if not final:
                        skipped.append({"bet_id": bet_id, "leg_id": leg_id, "reason": final_error})
                        continue

                    market = resolve_market(provider_market_key, raw_prop)
                    if not market:
                        skipped.append({"bet_id": bet_id, "leg_id": leg_id, "reason": "unsupported_market", "provider_market_key": provider_market_key, "prop": raw_prop})
                        continue

                    event_date = local_event_date(start_time)
                    if not event_date:
                        skipped.append({"bet_id": bet_id, "leg_id": leg_id, "reason": "missing_event_date"})
                        continue

                    if market.entity == "batter":
                        stat_value, error = get_single_hitter_result(cur, player_name, event_date, market)
                    else:
                        stat_value, error = get_single_pitcher_result(cur, player_name, event_date, market)

                    if error:
                        skipped.append({"bet_id": bet_id, "leg_id": leg_id, "reason": error, "market_key": market.key})
                        continue

                    result = compare_result(stat_value, side, line)
                    if result is None:
                        skipped.append({"bet_id": bet_id, "leg_id": leg_id, "reason": "unsupported_side_or_line"})
                        continue

                    cur.execute("""
                        UPDATE user_bet_legs
                        SET result=%s, status=%s
                        WHERE id=%s AND user_bet_id=%s
                    """, (result, result, leg_id, bet_id))

                    graded.append({
                        "bet_id": bet_id,
                        "leg_id": leg_id,
                        "result": result,
                        "stat_value": stat_value,
                        "line": float(line),
                        "player_name": player_name,
                        "market_key": market.key,
                        "market": market.display,
                    })

        ready_tickets = []
        with get_conn() as ticket_conn:
            with ticket_conn.cursor() as cur:
                cur.execute("""
                    SELECT ub.id, ub.bet_type, ubl.id,
                           LOWER(COALESCE(ubl.status,'pending')),
                           COALESCE(ubl.user_odds, ubl.odds),
                           COALESCE(ubl.sort_order, ubl.id)
                    FROM user_bets ub
                    JOIN user_bet_legs ubl ON ubl.user_bet_id=ub.id
                    WHERE ub.user_id=%s
                      AND LOWER(COALESCE(ub.status,'pending'))='pending'
                      AND UPPER(COALESCE(ub.sport,''))='MLB'
                    ORDER BY ub.id, COALESCE(ubl.sort_order, ubl.id)
                """, (user_id,))
                ticket_map = {}
                for bet_id, bet_type, leg_id, leg_status, leg_odds, leg_order in cur.fetchall():
                    ticket = ticket_map.setdefault(int(bet_id), {"bet_type": bet_type or "straight", "legs":[]})
                    ticket["legs"].append({"leg_id":int(leg_id),"status":str(leg_status or "pending").lower(),"odds":int(leg_odds) if leg_odds is not None else None,"sort_order":leg_order})

                for bet_id, ticket in ticket_map.items():
                    legs = ticket["legs"]
                    statuses = [leg["status"] for leg in legs]
                    ticket_result, adjusted_odds = "pending", None
                    removed_leg_ids = []

                    if "lost" in statuses:
                        ticket_result = "lost"
                    elif "pending" in statuses:
                        pass
                    elif statuses and all(v in {"push","void"} for v in statuses):
                        ticket_result = "push"
                    elif statuses and all(v in {"won","push","void"} for v in statuses):
                        active = [leg for leg in legs if leg["status"] == "won"]
                        removed_leg_ids = [leg["leg_id"] for leg in legs if leg["status"] in {"push","void"}]
                        if active and not any(leg["odds"] in (None,0) for leg in active):
                            combined_decimal = 1.0
                            for leg in active:
                                odds = int(leg["odds"])
                                combined_decimal *= 1 + odds/100 if odds > 0 else 1 + 100/abs(odds)
                            adjusted_odds = round((combined_decimal-1)*100) if combined_decimal >= 2 else round(-100/(combined_decimal-1))
                            ticket_result = "won"

                    if ticket_result != "pending":
                        ready_tickets.append({
                            "bet_id": bet_id,
                            "bet_type": ticket["bet_type"],
                            "result": ticket_result,
                            "leg_statuses": statuses,
                            "adjusted_odds": adjusted_odds,
                            "removed_leg_ids": removed_leg_ids,
                        })

        return {"success":True,"graded_legs":graded,"skipped_legs":skipped,"ready_tickets":ready_tickets}
    finally:
        conn.close()

def grade_pending_mlb_straights(user_id):
    return grade_pending_mlb_bets(user_id)
