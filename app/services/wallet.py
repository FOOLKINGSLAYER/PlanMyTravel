from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


class WalletError(ValueError):
    pass


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise WalletError("Enter a valid amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise WalletError("Amount must be greater than zero.")
    return amount


def wallet_balance(connection: Any, user_id: int) -> Decimal:
    row = connection.execute(
        "SELECT balance FROM wallets WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return Decimal(str(row[0])) if row else Decimal("0.00")


def add_funds(connection: Any, user_id: int, amount: Any) -> Decimal:
    value = _amount(amount)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO wallets (user_id, balance, currency) VALUES (?, 0, 'INR')",
            (user_id,),
        )
        connection.execute(
            "UPDATE wallets SET balance = balance + ? WHERE user_id = ?",
            (str(value), user_id),
        )
        wallet = connection.execute(
            "SELECT id FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        connection.execute(
            """INSERT INTO wallet_transactions
               (wallet_id, type, amount, status, created_at)
               VALUES (?, 'top_up', ?, 'completed', CURRENT_TIMESTAMP)""",
            (wallet[0], str(value)),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return wallet_balance(connection, user_id)


def charge(
    connection: Any,
    user_id: int,
    amount: Any,
    trip_id: int | None = None,
    booking_id: int | None = None,
) -> Decimal:
    value = _amount(amount)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO wallets (user_id, balance, currency) VALUES (?, 0, 'INR')",
            (user_id,),
        )
        result = connection.execute(
            "UPDATE wallets SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (str(value), user_id, str(value)),
        )
        if result.rowcount != 1:
            raise WalletError("Your wallet balance is too low for this booking.")
        wallet = connection.execute(
            "SELECT id FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        connection.execute(
            """INSERT INTO wallet_transactions
               (wallet_id, trip_id, booking_id, type, amount, status, created_at)
               VALUES (?, ?, ?, 'payment', ?, 'completed', CURRENT_TIMESTAMP)""",
            (wallet[0], trip_id, booking_id, str(value)),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return wallet_balance(connection, user_id)
