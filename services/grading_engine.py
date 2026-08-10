from decimal import Decimal, ROUND_HALF_UP
from database import get_conn


VALID_RESULTS = {"won", "lost", "push", "void"}


def money(value):
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def grade_bet(
    bet_id,
    result,
    user_id,
    update_legs=True,
    odds_override=None
):
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
                    odds_override
                    if odds_override is not None
                    else user_combined_odds
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
                    (
                        f"Bet #{bet_id} graded {result}"
                        + (
                            f" at adjusted odds {odds_override:+d}"
                            if odds_override is not None
                            else ""
                        )
                    )
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


def regrade_bet(
    bet_id,
    result,
    user_id,
    update_legs=True,
    odds_override=None,
    reason="Provider result correction",
    leg_results=None
):
    """
    Correct an already-settled bet safely.

    Unlike grade_bet(), this function does NOT re-apply the entire payout.
    It calculates:

        correction_amount = new_profit - old_profit

    and applies only that difference to the bankroll.

    Example:
        Old settlement: lost $10  -> old_profit = -10.00
        Correct result: won +$8   -> new_profit = +8.00
        Bankroll correction       -> +$18.00

    That restores the $10 loss and then adds the correct $8 profit.

    Use update_legs=False for parlays when their individual leg results
    have already been updated separately.
    """
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

                # Lock the bet to prevent concurrent settlement/correction.
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
                        result,
                        profit
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
                    current_result,
                    current_profit
                ) = bet

                current_status = (
                    str(current_status or "pending")
                    .strip()
                    .lower()
                )

                current_result = (
                    str(current_result or current_status or "")
                    .strip()
                    .lower()
                )

                if current_status == "pending":
                    return {
                        "success": False,
                        "error": (
                            "This bet is still pending. "
                            "Use grade_bet() for first settlement."
                        )
                    }

                if current_status not in VALID_RESULTS:
                    return {
                        "success": False,
                        "error": "Current bet status cannot be corrected."
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

                old_profit = money(current_profit)

                # Prefer corrected/adjusted parlay odds if supplied.
                odds = (
                    odds_override
                    if odds_override is not None
                    else user_combined_odds
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

                # Calculate what the bet SHOULD have contributed.
                if result == "won":
                    if odds > 0:
                        new_profit = (
                            stake
                            * Decimal(odds)
                            / Decimal("100")
                        )
                    else:
                        new_profit = (
                            stake
                            * Decimal("100")
                            / Decimal(abs(odds))
                        )

                    new_profit = money(new_profit)

                elif result == "lost":
                    new_profit = money(-stake)

                else:
                    # Push / Void.
                    new_profit = money(0)

                # If nothing financial/result-wise changes, do nothing.
                if (
                    current_result == result
                    and old_profit == new_profit
                    and not leg_results
                ):
                    return {
                        "success": True,
                        "bet_id": bet_id,
                        "changed": False,
                        "result": result,
                        "old_result": current_result,
                        "old_profit": float(old_profit),
                        "new_profit": float(new_profit),
                        "correction_amount": 0.0,
                        "message": "Bet already has this settlement."
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

                # Apply ONLY the difference between old and new settlement.
                correction_amount = money(
                    new_profit - old_profit
                )

                balance_after = money(
                    balance_before + correction_amount
                )

                # Update the ticket.
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
                    new_profit,
                    bet_id,
                    user_id
                ))

                # Update leg results atomically with the ticket correction.
                #
                # For parlays, leg_results should be a list like:
                # [{"leg_id": 10, "result": "won"}, ...]
                #
                # For straight bets, update_legs=True keeps the original
                # one-leg synchronization behavior.
                if leg_results:
                    for leg_item in leg_results:
                        leg_id = leg_item.get("leg_id")
                        leg_result = str(
                            leg_item.get("result") or ""
                        ).strip().lower()

                        if (
                            not leg_id
                            or leg_result not in VALID_RESULTS
                        ):
                            return {
                                "success": False,
                                "error": "Invalid corrected leg result."
                            }

                        cur.execute("""
                            UPDATE user_bet_legs
                            SET
                                status = %s,
                                result = %s
                            WHERE id = %s
                              AND user_bet_id = %s
                        """, (
                            leg_result,
                            leg_result,
                            leg_id,
                            bet_id
                        ))

                        if cur.rowcount != 1:
                            return {
                                "success": False,
                                "error": (
                                    "A corrected parlay leg "
                                    "could not be updated."
                                )
                            }

                elif update_legs:
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

                # Apply bankroll correction.
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

                # Preserve history: never delete the original settlement.
                # Add a separate correction transaction.
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
                    "CORRECTION",
                    correction_amount,
                    balance_before,
                    balance_after,
                    units,
                    (
                        f"Bet #{bet_id} corrected "
                        f"{current_result} -> {result}. "
                        f"Old P/L {old_profit:+.2f}; "
                        f"new P/L {new_profit:+.2f}. "
                        f"{reason}"
                    )
                ))

                return {
                    "success": True,
                    "bet_id": bet_id,
                    "changed": True,
                    "old_result": current_result,
                    "result": result,
                    "old_profit": float(old_profit),
                    "new_profit": float(new_profit),
                    "correction_amount": float(correction_amount),
                    "balance_before": float(balance_before),
                    "balance_after": float(balance_after)
                }

    except Exception as exc:
        print(
            f"regrade_bet error "
            f"(bet={bet_id}, user={user_id}):",
            exc
        )

        return {
            "success": False,
            "error": "Bet could not be corrected."
        }

    finally:
        conn.close()
