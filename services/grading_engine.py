from decimal import Decimal, ROUND_HALF_UP
from database import get_conn


VALID_RESULTS = {"won", "lost", "push", "void"}


def money(value):
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def grade_bet(bet_id, result, user_id, update_legs=True):
    result = str(result or "").strip().lower()

    if result not in VALID_RESULTS:
        return {
            "success": False,
            "error": "Invalid result."
        }

    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:

                # Lock bet so it cannot be graded twice at the same time.
                cur.execute("""
                    SELECT
                        id,
                        user_id,
                        bankroll_id,
                        ticket_id,
                        stake,
                        odds_taken,
                        combined_odds,
                        user_combined_odds,
                        units,
                        status,
                        result
                    FROM user_bets
                    WHERE id = %s
                      AND user_id = %s
                    FOR UPDATE
                """, (
                    bet_id,
                    user_id
                ))

                bet = cur.fetchone()

                if not bet:
                    return {
                        "success": False,
                        "error": "Bet not found."
                    }

                (
                    db_bet_id,
                    db_user_id,
                    bankroll_id,
                    ticket_id,
                    stake,
                    odds_taken,
                    combined_odds,
                    user_combined_odds,
                    units,
                    current_status,
                    current_result
                ) = bet

                current_status = (
                    str(current_status or "pending")
                    .strip()
                    .lower()
                )

                if current_status != "pending":
                    return {
                        "success": False,
                        "error": "This bet has already been graded."
                    }

                if not bankroll_id:
                    return {
                        "success": False,
                        "error": "This bet has no bankroll attached."
                    }

                stake = money(stake)

                if stake <= 0:
                    return {
                        "success": False,
                        "error": "This bet has an invalid stake."
                    }

                # Prefer user's actual parlay odds, then combined,
                # then straight-bet odds.
                odds = (
                    user_combined_odds
                    if user_combined_odds is not None
                    else combined_odds
                    if combined_odds is not None
                    else odds_taken
                )

                if result == "won" and odds is None:
                    return {
                        "success": False,
                        "error": "Winning bet has no odds."
                    }

                if odds is not None:
                    odds = int(odds)

                if result == "won" and odds == 0:
                    return {
                        "success": False,
                        "error": "Invalid American odds."
                    }

                # Lock bankroll.
                cur.execute("""
                    SELECT
                        id,
                        current_balance,
                        unit_percentage,
                        auto_resize
                    FROM user_bankrolls
                    WHERE id = %s
                      AND user_id = %s
                    FOR UPDATE
                """, (
                    bankroll_id,
                    user_id
                ))

                bankroll = cur.fetchone()

                if not bankroll:
                    return {
                        "success": False,
                        "error": "Bankroll not found."
                    }

                balance_before = money(bankroll[1])

                # --------------------------------------------------
                # CALCULATE PROFIT
                # --------------------------------------------------

                if result == "won":

                    if odds > 0:
                        profit = stake * Decimal(odds) / Decimal("100")
                    else:
                        profit = (
                            stake
                            * Decimal("100")
                            / Decimal(abs(odds))
                        )

                    profit = money(profit)

                elif result == "lost":

                    profit = money(-stake)

                else:
                    # Push / Void
                    profit = money(0)

                balance_after = money(
                    balance_before + profit
                )

                # --------------------------------------------------
                # UPDATE BET
                # --------------------------------------------------

                cur.execute("""
                    UPDATE user_bets
                    SET
                        status = %s,
                        result = %s,
                        profit = %s,
                        settled_at = NOW()
                    WHERE id = %s
                      AND user_id = %s
                """, (
                    result,
                    result,
                    profit,
                    bet_id,
                    user_id
                ))

                # --------------------------------------------------
                # UPDATE LEGS
                #
                # For V1 manual straight bets this keeps the one
                # leg synchronized. Parlays can later be graded
                # leg-by-leg without changing the bankroll engine.
                # --------------------------------------------------

                if update_legs:
                    cur.execute("""
                        UPDATE user_bet_legs
                        SET
                            status = %s,
                            result = %s
                        WHERE user_bet_id = %s
                    """, (
                        result,
                        result,
                        bet_id
                    ))

                # --------------------------------------------------
                # UPDATE BANKROLL
                # --------------------------------------------------

                cur.execute("""
                    UPDATE user_bankrolls
                    SET
                        current_balance = %s,
                        updated_at = NOW()
                    WHERE id = %s
                      AND user_id = %s
                """, (
                    balance_after,
                    bankroll_id,
                    user_id
                ))

                # --------------------------------------------------
                # TRANSACTION LOG
                # --------------------------------------------------

                cur.execute("""
                    INSERT INTO bankroll_transactions (
                        bankroll_id,
                        user_id,
                        bet_id,
                        ticket_id,
                        transaction_type,
                        amount,
                        balance_before,
                        balance_after,
                        units,
                        notes,
                        created_at
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
                        NOW()
                    )
                """, (
                    bankroll_id,
                    user_id,
                    bet_id,
                    ticket_id,
                    result.upper(),
                    profit,
                    balance_before,
                    balance_after,
                    units,
                    f"Bet #{bet_id} graded {result}"
                ))

                return {
                    "success": True,
                    "bet_id": bet_id,
                    "result": result,
                    "profit": float(profit),
                    "balance_before": float(balance_before),
                    "balance_after": float(balance_after)
                }

    except Exception as exc:
        print(
            f"grade_bet error "
            f"(bet={bet_id}, user={user_id}):",
            exc
        )

        return {
            "success": False,
            "error": "Bet could not be graded."
        }

    finally:
        conn.close()